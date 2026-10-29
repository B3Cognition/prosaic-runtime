"""One host-admitted custom tool, with no provider request or metering intent."""
import json
import time
from uuid import uuid4

from jsonschema.exceptions import ValidationError

from .accounting import ResolvedContext, resolve_context
from .budgets import InvocationBudget, InvocationBudgetExceeded
from .events import Cancelled, check_cancelled, emit, event_context
from .execution_budget import active_budgets, check_budgets, reserve_dispatch
from .operation_context import InvocationScope
from .policy import BUILTIN_TOOLS, RunPolicy
from .telemetry import ObserverEmitter, observation_context, execution_started
from .tool_effects import ToolEffectFailure, admit_tool_effects
from .tool_outcomes import ToolOutcome
from .tools import ToolDeadlineExceeded, depth, execute_custom_tool, parse_arguments
from .types import Result


def run_tool(runtime, name, arguments, *, policy=None, cwd='.', on_event=None,
             cancelled=None, operation_context=None, tool_journal=None, observer=None,
             context=None, accounting=None):
    # Snapshot authority before any observer or trusted callback can change it.
    config, custom, cli = runtime.config, dict(runtime._custom_tools), frozenset(runtime._cli_tools)
    grants = frozenset(config.allowed_tools)
    policy = policy if policy is not None else RunPolicy(timeout_s=config.limits.timeout_s,
                                                        max_tool_rounds=config.limits.max_tool_rounds)
    if type(policy) is not RunPolicy:
        raise ValueError('invalid direct tool policy')
    scope = InvocationScope(str(uuid4())) if operation_context is None else operation_context
    emitter = ObserverEmitter(observer, source='runtime', scope=scope)
    budget, shared = InvocationBudget(policy), active_budgets()
    started = time.monotonic()
    deadline = min([started + policy.timeout_s, *(b.deadline for b in shared)])
    result, resolved = None, None
    outcome = ToolOutcome('unclassified')
    tool_started = None

    def finish(reason=None, *, value=None, timed_out=False):
        return Result(130 if reason == 'cancelled' else 1 if reason else 0,
                      '' if reason else json.dumps(value, ensure_ascii=False, allow_nan=False),
                      reason or '', token_usage=0, cost_usd=0, timed_out=timed_out,
                      metadata={'direct_tool_v1': {'model_free': True, 'provider_requests': 0},
                                **({'failure_reason': reason} if reason else {})})

    def boundary():
        check_budgets(shared)
        check_cancelled()
        from .conformance import _check_suite_deadline
        _check_suite_deadline()
        if time.monotonic() >= deadline:
            raise ToolDeadlineExceeded('invocation deadline exceeded')

    def record_outcome(value):
        nonlocal outcome
        outcome = ToolOutcome.from_dict(value.to_dict())

    # Resolve billing/actor attribution before observation can mutate host defaults.
    recorder = accounting if accounting is not None else runtime.accounting
    enabled = recorder is not None or context is not None or runtime.context_defaults is not None
    try:
        if enabled:
            resolved = resolve_context(context, getattr(recorder, 'defaults', None) or runtime.context_defaults)
            resolved = ResolvedContext.from_dict(resolved.to_dict())
    except (ValueError, TypeError, AttributeError):
        result = finish('admission_failure')

    # Own literal arguments and admit effect requirements before any host hook.
    if result is None:
        if type(name) is not str or name not in custom and name not in BUILTIN_TOOLS:
            result = finish('not_registered')
        elif name in BUILTIN_TOOLS or name in cli:
            result = finish('unsupported_direct_tool')
        elif name not in grants or name not in policy.allowed_tools:
            result = finish('not_granted')
        else:
            tool = custom[name]
            try:
                if type(arguments) is not dict:
                    raise ValueError('invalid arguments')
                depth(arguments)
                raw = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
                parse_arguments(raw, tool.parameters, min(tool.max_argument_bytes, policy.max_input_bytes))
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, ValidationError):
                result = finish('invalid_arguments')
            if result is None:
                try:
                    admit_tool_effects(custom, {name}, scope, tool_journal)
                except (ValueError, TypeError, AttributeError):
                    result = finish('admission_failure')

    with observation_context(emitter, custom), event_context(on_event, cancelled):
        try:
            emitter.emit('invocation_started')
            boundary()
            if result is None:
                boundary()
                # Check local capacity before atomically debiting shared budgets.
                budget._check_reported_allowance()
                if policy.max_tool_calls is not None and budget.tool_calls >= policy.max_tool_calls:
                    raise InvocationBudgetExceeded('tool_call_limit')
                reserve_dispatch(shared, tool=True)
                budget.before_tool()
                execution_started()
                tool_started = time.monotonic()
                emit('tool_started', version=1, name=name, tool_version=tool.version,
                     call_id='direct-0', turn=0)
                payload = execute_custom_tool(tool, raw, check_boundary=boundary, scope=scope,
                    deadline=deadline, cancelled=cancelled if cancelled is not None else lambda: False,
                    call_index=0, tool_journal=tool_journal, accounting_context=resolved,
                    on_outcome=record_outcome)
                result = (finish(value=payload['result']) if payload['status'] == 'ok'
                          else finish(payload['error']))
        except Cancelled:
            result = finish('cancelled')
        except ToolDeadlineExceeded:
            result = finish('invocation_timeout', timed_out=True)
        except InvocationBudgetExceeded as exc:
            result = finish(exc.reason)
        except ToolEffectFailure as exc:
            result = finish(exc.reason)
        except Exception:
            result = finish('critical_hook_error' if emitter.critical_hook_failed else 'execution_failure')
        finally:
            if tool_started is not None and not emitter.critical_hook_failed:
                try:
                    # Terminal evidence must still reach the host after cancellation.
                    with event_context(on_event):
                        emit('tool_completed', version=1, name=name, tool_version=tool.version,
                             call_id='direct-0', turn=0,
                             status='ok' if result is not None and result.exit_code == 0 else 'error',
                             duration_ms=(time.monotonic() - tool_started) * 1000,
                             tool_outcome=outcome.to_dict())
                except Exception:
                    result = finish('critical_hook_error')
            if result is not None:
                result.metadata['invocation_budgets_v1'] = budget.metadata()
                result.metadata['tool_outcome'] = outcome.to_dict()
                if resolved is not None:
                    result.metadata['accounting_v1'] = {'context': resolved.to_dict(),
                        'namespace': getattr(recorder, 'namespace', None),
                        'environment': getattr(recorder, 'environment', None), 'provider_call_ids': []}
            reason = result.metadata.get('failure_reason') if result is not None else None
            terminal = ('critical_hook_error' if emitter.critical_hook_failed else
                        'cancelled' if reason == 'cancelled' else
                        'timed_out' if result is not None and result.timed_out else
                        'budget_failure' if reason in InvocationBudgetExceeded.REASONS else
                        'completed' if result is not None and result.exit_code == 0 else 'execution_failure')
            emitter.emit('invocation_completed', outcome=terminal,
                         duration_ms=(time.monotonic() - started) * 1000,
                         exit_code=result.exit_code if result is not None else None,
                         token_usage=0, reason=reason)
    return result
