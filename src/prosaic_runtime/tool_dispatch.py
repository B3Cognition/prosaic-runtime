"""Custom-tool phases shared by sequential dispatch and the parallel coordinator.

Only invoke_prepared_tool may run off the invocation's owner thread. Journal,
authorization, lifecycle and completion callbacks belong to prepare/start/settle.
"""
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import time
from types import MappingProxyType
from jsonschema.exceptions import ValidationError

from .events import Cancelled
from .operation_context import ToolExecutionContext, bounded_reference
from .tool_effects import (ToolEffectFailure, ToolFailure, canonical_json, failure_outcome,
                          snapshot_outcome, validate_claim)
from .tool_outcomes import ToolOutcome
from .terminal import TerminalCompletion, TerminalCompletionError


@dataclass(frozen=True)
class WorkerSettlementV1:
    call_id: str
    call_index: int
    batch_index: int
    code: str
    _encoded: bytes | None = field(default=None, repr=False)
    handler_entered: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    replayed: bool = False
    exception: BaseException | None = field(default=None, repr=False, compare=False)
    terminal: object = field(default=None, repr=False, compare=False)
    journal_outcome: dict | None = field(default=None, repr=False, compare=False)

    @property
    def outcome(self):
        return json.loads(self._encoded) if self._encoded is not None else None


@dataclass(frozen=True)
class PreparedToolCallV1:
    call_id: str
    call_index: int
    batch_index: int
    tool: object = field(repr=False)
    _arguments: bytes = field(repr=False)
    context: ToolExecutionContext | None = field(repr=False)
    options: object = field(repr=False, compare=False)
    signature: str | None = None
    operation_key: str | None = None
    claim_token: str | None = field(default=None, repr=False)

    @property
    def arguments(self): return json.loads(self._arguments)


def _report(options, code, replayed=False):
    options.get('_phase_state', {})['code'] = code
    callback = options.get('on_outcome')
    if callback is not None: callback(ToolOutcome(code, replayed))


def _complete(options, outcome, status='completed', **refs):
    state=options.get('_phase_state', {})
    state.update(status=status, **refs)
    callback = options.get('on_completion')
    if callback is not None: callback(outcome, status, **refs)


def _settlement(call_id, call_index, batch_index, code, outcome=None, **kw):
    encoded=None
    if outcome is not None:
        try:encoded=json.dumps(outcome,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        except UnicodeEncodeError:
            # Legacy ordinary results may contain escaped lone surrogates; their
            # admission already charges the ASCII encoding against the tool cap.
            encoded=json.dumps(outcome,separators=(',',':'),allow_nan=False).encode('utf-8')
    return WorkerSettlementV1(call_id,call_index,batch_index,code,encoded,**kw)


def prepare_custom_tool_call(tool, raw_arguments, *, call_id, call_index, batch_index, options):
    from .tools import parse_arguments, ToolDeadlineExceeded
    options.setdefault('_phase_state', {})
    callback = options.get('on_outcome')
    if callback is not None and not callable(callback): raise ValueError('invalid tool outcome observer')
    check = options['check_boundary']; check()
    try:
        arguments = parse_arguments(raw_arguments, tool.parameters, tool.max_argument_bytes)
    except (TypeError, ValueError, RecursionError, OverflowError, ValidationError):
        _report(options, 'invalid_arguments')
        return _settlement(call_id,call_index,batch_index,'invalid_arguments',{'status':'error','error':'invalid_arguments'})
    signature = (hashlib.sha256(canonical_json(dict(tool_name=tool.name,tool_version=tool.version,arguments=arguments)).encode()).hexdigest()
        if options.get('on_completion') is not None else None)
    context = None
    if tool.with_context:
        try: argument_digest = hashlib.sha256(canonical_json(arguments).encode()).hexdigest()
        except (TypeError, ValueError, RecursionError, OverflowError):
            _report(options, 'invalid_arguments')
            return _settlement(call_id,call_index,batch_index,'invalid_arguments',{'status':'error','error':'invalid_arguments'})
        context = ToolExecutionContext(options.get('scope'),tool.name,tool.version,call_index,
            argument_digest,options.get('deadline'),
            options.get('cancelled', lambda:False),accounting_context=options.get('accounting_context'))
    if tool.authorize is not None:
        try: allowed = (tool.authorize(deepcopy(arguments), context) if tool.with_context else tool.authorize(deepcopy(arguments))) is True
        except (Cancelled, ToolDeadlineExceeded): raise
        except Exception: allowed = False
        check()
        if not allowed:
            _report(options,'authorization_denied')
            return _settlement(call_id,call_index,batch_index,'authorization_denied',{'status':'error','error':'authorization_denied'})
    from .sub_agents import is_sub_agent_tool, digest
    from .sub_agent_execution import current_execution, decode_receipt, JOURNAL_BYTES
    delegation = current_execution() if is_sub_agent_tool(tool) else None
    if delegation is not None and not delegation.preflight(context):
        check()
        _report(options,'authorization_denied')
        return _settlement(call_id,call_index,batch_index,'authorization_denied',
                           {'status':'error','error':'delegate_unavailable'})
    check(); key = token = None
    if tool.operation_key is not None:
        from .tools import ToolDeadlineExceeded
        try: key = tool.operation_key(deepcopy(arguments),context)
        except (Cancelled,ToolDeadlineExceeded): raise
        except Exception:
            _report(options,'tool_identity_conflict'); raise ToolEffectFailure('tool_identity_conflict') from None
        if not bounded_reference(key,256):
            _report(options,'tool_identity_conflict'); raise ToolEffectFailure('tool_identity_conflict')
        check(); context = replace(context,operation_key=key)
        if signature is None:
            signature=hashlib.sha256(canonical_json(dict(tool_name=tool.name,tool_version=tool.version,arguments=arguments)).encode()).hexdigest()
        if delegation is not None:
            signature = digest([signature, delegation.frame.identity])
        refs = dict(operation_key=key,signature=signature)
        if options.get('_await_operation') is not None:
            waited=options['_await_operation'](context.scope.operation_namespace,key,signature)
            if waited and tool.authorize is not None:
                try: allowed=tool.authorize(deepcopy(arguments),context) is True
                except (Cancelled,ToolDeadlineExceeded):raise
                except Exception:allowed=False
                check()
                if not allowed:
                    _report(options,'authorization_denied')
                    return _settlement(call_id,call_index,batch_index,'authorization_denied',{'status':'error','error':'authorization_denied'})
        try: claim = options['tool_journal'].claim(context.scope.operation_namespace,key,signature)
        except Exception:
            _report(options,'tool_journal_failed'); _complete(options,None,'uncertain',**refs)
            raise ToolEffectFailure('tool_journal_failed') from None
        try: replay = validate_claim(claim, signature, JOURNAL_BYTES if delegation is not None else tool.max_result_bytes)
        except ToolEffectFailure as exc:
            _report(options,exc.reason); _complete(options,None,'uncertain',**refs); raise
        if claim.state=='uncertain':
            _report(options,'tool_effect_uncertain'); _complete(options,None,'uncertain',**refs)
            raise ToolEffectFailure('tool_effect_uncertain')
        if claim.state=='replay':
            if delegation is not None:
                document, replay = decode_receipt(delegation, context, tool, replay)
                if (not delegation.authorize_launch(tool.name, arguments, context)
                        or not delegation.authorize_result(tool.name, context, tuple(document['sources']), 'replay')):
                    _report(options, 'authorization_denied')
                    return _settlement(call_id, call_index, batch_index, 'authorization_denied',
                                       {'status': 'error', 'error': 'authorization_denied'})
                delegation.record(context, document)
            from .mcp import is_mcp_tool
            if is_mcp_tool(tool):
                # A journal lookup can block while the caller's grant changes.
                allowed = tool.authorize(arguments, context)
                check()
                if not allowed:
                    _report(options, 'authorization_denied')
                    return _settlement(call_id, call_index, batch_index, 'authorization_denied',
                                       {'status': 'error', 'error': 'authorization_denied'})
            if tool.result_contract is not None:
                from .tool_result_contract import validate_tool_result_outcome
                try: replay=validate_tool_result_outcome(tool,replay)
                except ValueError:
                    _report(options,'tool_journal_failed'); raise ToolEffectFailure('tool_journal_failed') from None
            code='ok' if replay['status']=='ok' else 'controlled_failure'
            _report(options,code,True); _complete(options,replay,'replayed',**refs); check()
            return _settlement(call_id,call_index,batch_index,code,replay,replayed=True)
        token=claim.claim_token
        try: check()
        except BaseException:
            if options.get('_parallel_mode'):
                _report(options,'tool_effect_uncertain'); _complete(options,None,'uncertain',**refs)
                raise ToolEffectFailure('tool_effect_uncertain') from None
            raise
    return PreparedToolCallV1(call_id,call_index,batch_index,tool,json.dumps(arguments,allow_nan=False).encode(),
        context,MappingProxyType(dict(options)),signature if options.get('on_completion') is not None or key is not None else None,key,token)


def start_prepared_tool(call):
    callback=call.options.get('_before_handler')
    if callback is not None: callback(call.arguments)


def invoke_prepared_tool(call):
    from .tools import ToolExecutionError,ToolDeadlineExceeded,bounded_result
    tool=call.tool; started=time.monotonic()
    def result(code,outcome=None,**kw):
        return _settlement(call.call_id,call.call_index,call.batch_index,code,outcome,
            handler_entered=True,started_at=started,finished_at=time.monotonic(),**kw)
    try:
        value=tool.handler(call.arguments,call.context) if tool.with_context else tool.handler(call.arguments)
    except (Cancelled,ToolDeadlineExceeded) as exc:
        return result('tool_effect_uncertain' if call.operation_key is not None else 'unclassified',exception=exc)
    except ToolExecutionError as exc:
        return result('tool_effect_uncertain' if call.operation_key is not None else 'execution_failed',
            {'status':'error','error':exc.code})
    except Exception:
        return result('tool_effect_uncertain' if call.operation_key is not None else 'handler_error',
            {'status':'error','error':'handler_error'})
    if type(value) is TerminalCompletion and call.operation_key is None:
        return result('ok',terminal=value)
    try:
        from .sub_agents import is_sub_agent_tool
        if is_sub_agent_tool(tool):
            from .sub_agent_execution import current_execution, encode_receipt, decode_receipt
            execution = current_execution()
            envelope = encode_receipt(execution, call.context, tool, value)
            _, outcome = decode_receipt(execution, call.context, tool, envelope)
            return result('ok' if outcome['status'] == 'ok' else 'controlled_failure',
                          outcome, journal_outcome=envelope)
        if tool.result_contract is not None:
            from .tool_result_contract import normalize_tool_result
            outcome=normalize_tool_result(tool,value)
            return result('ok' if outcome['status']=='ok' else 'controlled_failure',outcome)
        if call.operation_key is not None:
            outcome=snapshot_outcome(failure_outcome(value),tool.max_result_bytes)
            return result('ok' if outcome['status']=='ok' else 'controlled_failure',outcome)
        codes=[]; outcome=bounded_result(value,tool.max_result_bytes,on_outcome=lambda o:codes.append(o.code))
        return result(codes[0],outcome)
    except Exception as exc:
        if call.operation_key is not None: return result('tool_effect_uncertain')
        from .tool_result_contract import ToolResultValidationError
        if isinstance(exc,ToolResultValidationError): return result(exc.code,{'status':'error','error':exc.code})
        raise


def settle_custom_tool_call(call, settlement, *, on_completion=None):
    options=call.options; check=options['check_boundary']; outcome=settlement.outcome; code=settlement.code
    if call.operation_key is not None:
        refs=dict(operation_key=call.operation_key,signature=call.signature)
        if code not in {'ok','controlled_failure'} or settlement.exception is not None:
            _report(options,'tool_effect_uncertain'); _complete(options,None,'uncertain',**refs)
            raise ToolEffectFailure('tool_effect_uncertain')
        try:
            committed = settlement.journal_outcome if settlement.journal_outcome is not None else outcome
            options['tool_journal'].commit(call.context.scope.operation_namespace, call.operation_key,
                                           call.claim_token, deepcopy(committed))
        except Exception:
            _report(options,'tool_journal_failed'); _complete(options,None,'uncertain',**refs)
            raise ToolEffectFailure('tool_journal_failed') from None
        if settlement.journal_outcome is not None:
            from .sub_agent_execution import current_execution, decode_receipt
            delegation = current_execution()
            document, _ = decode_receipt(delegation, call.context, call.tool, settlement.journal_outcome)
            if not delegation.authorize_result(call.tool.name, call.context, tuple(document['sources']), 'result'):
                outcome = {'status': 'error', 'error': 'authorization_denied'}
                code = 'controlled_failure'
                settlement = _settlement(call.call_id, call.call_index, call.batch_index, code, outcome,
                                         handler_entered=settlement.handler_entered)
            else:
                delegation.record(call.context, document)
        options.get('_phase_state',{}).update(status='completed',code=code,**refs)
        _report(options,code); _complete(options,outcome,'completed',**refs); check(); return settlement
    if settlement.exception is not None:
        if options.get('_parallel_mode'):
            _report(options,code); _complete(options,None,'uncertain')
        raise settlement.exception
    if settlement.terminal is not None:
        if not call.tool.terminal or options.get('on_terminal') is None:
            _report(options,'invalid_result'); raise TerminalCompletionError('terminal_completion_not_granted')
        options['on_terminal'](settlement.terminal,call.arguments)
        outcome={'status':'ok','result':settlement.terminal.output}
        settlement=_settlement(call.call_id,call.call_index,call.batch_index,'ok',outcome,
            handler_entered=True,started_at=settlement.started_at,finished_at=settlement.finished_at)
    known=code in {'ok','controlled_failure'}
    options.get('_phase_state',{}).update(status='completed' if known else 'uncertain',code=code)
    _report(options,code)
    if options.get('_parallel_mode') and not known:
        _complete(options,None,'uncertain')
    elif code not in {'handler_error','execution_failed'}:
        callback=on_completion or options.get('on_completion')
        if callback is not None: callback(outcome,signature=call.signature)
    check(); return settlement
