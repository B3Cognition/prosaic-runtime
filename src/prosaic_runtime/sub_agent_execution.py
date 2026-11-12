"""Serial child execution and private journal settlement for admitted delegates."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json

from jsonschema import Draft202012Validator, ValidationError

from .conversation import TurnAdmissionError, encode_owned
from .events import Cancelled
from .execution_budget import ExecutionBudget, active_budgets
from .operation_context import InvocationScope
from .sub_agents import PROVENANCE_BYTES, RESULT_BYTES, digest, source_references
from .tool_contracts import EvidenceAuthorizerV1, EvidenceDraftV1, ToolContractBindingsV1, ToolResultV1
from .tool_effects import ToolEffectFailure, ToolFailure, snapshot_outcome
from .tool_result_contract import normalize_tool_result, select_pointer
from .tools import CustomTool, ToolDeadlineExceeded

JOURNAL_BYTES = RESULT_BYTES + PROVENANCE_BYTES + 2048
_active = ContextVar('prosaic_delegation_execution', default=None)


def current_execution():
    return _active.get()


@dataclass(frozen=True)
class ChildResult:
    value: ToolResultV1 | ToolFailure
    sources: tuple


class DelegationExecution:
    def __init__(self, frame, access, runtime, config, recorder, policy, cancelled):
        self.frame, self.access, self.runtime = frame, access, runtime
        self.config, self.recorder = config, recorder
        self.launches = 0
        self.records = []
        self._exposed = []
        limits = frame.policy
        self.root = ExecutionBudget(timeout_s=policy.timeout_s,
            max_provider_requests=limits.max_provider_requests,
            max_tool_calls=limits.max_tool_calls, max_reported_tokens=limits.max_reported_tokens,
            cancelled=lambda: (cancelled is not None and cancelled()) or not self.continuation_granted())

    def owner_valid(self):
        try:
            return self.access.check_owner() is True
        except Exception:
            return False

    def continuation_granted(self):
        return self.owner_valid() and all(
            self.authorize_result(context.tool_name, context, refs, 'parentUse')
            for context, refs in self._exposed
        )

    @contextmanager
    def activate(self):
        if current_execution() is not None:
            raise TurnAdmissionError('invalidPolicy')
        token = _active.set(self)
        try:
            with self.root.activate():
                yield
        finally:
            _active.reset(token)

    def can_launch(self):
        return (self.launches < self.frame.policy.max_delegations
                and all(b.provider_remaining > 1 for b in active_budgets()))

    def authorize_result(self, alias, context, refs, purpose):
        try:
            return self.owner_valid() and self.access.authorize_result(alias, context, refs, purpose) is True
        except Exception:
            return False

    def authorize_launch(self, alias, arguments, context):
        try:
            return (context.scope == self.frame.scope and self.owner_valid()
                    and self.access.authorize_launch(alias, arguments, context) is True)
        except Exception:
            return False

    def _child_tools(self, spec, context, sources):
        def wrapped(read):
            tool = read.tool

            def invoke(arguments, tool_context=None):
                value = tool.handler(arguments, tool_context) if tool.with_context else tool.handler(arguments)
                if type(value) is ToolFailure:
                    return value
                outcome = normalize_tool_result(tool, value)
                if not read.public:
                    refs = source_references(read.source_refs(outcome))
                    if not refs:
                        raise ValueError('unclassified protected read')
                    sources[:] = source_references([*sources, *refs])
                    if not self.authorize_result(spec.alias, context, tuple(sources), 'childUse'):
                        raise ValueError('protected child result unavailable')
                return value

            return CustomTool(tool.name, tool.description, tool.parameters, invoke, tool.version,
                with_context=tool.with_context, authorize=tool.authorize, result_contract=tool.result_contract,
                max_argument_bytes=tool.max_argument_bytes, max_result_bytes=tool.max_result_bytes)

        return {read.tool.name: wrapped(read) for read in spec.reads}

    def invoke(self, spec, arguments, context):
        entry = self.frame.projections[spec.alias]
        sources = entry['sources']
        unavailable = ChildResult(ToolFailure('delegate_unavailable'), ())
        if (not self.can_launch() or not self.authorize_launch(spec.alias, arguments, context)
                or not self.authorize_result(spec.alias, context, tuple(sources), 'childUse')):
            return unavailable
        try:
            providers = min(spec.policy.max_provider_requests, *(b.provider_remaining - 1 for b in active_budgets()))
            timeout = context.remaining_timeout(spec.policy.timeout_s)
            policy = replace(spec.policy, max_provider_requests=providers, timeout_s=timeout)
            child_scope = InvocationScope(child_identity(context), context.scope.run_id,
                context.scope.step_id, context.scope.operation_namespace)
            # The admitted route is already reduced to the parent's exact profile.
            artifact = spec.artifact
            tier = artifact.frontmatter.get('model_tier')
            config = replace(self.config, allowed_tools=policy.allowed_tools,
                             routes={tier: self.config.default_profile} if tier is not None else {},
                             required_guards={})
            child = type(self.runtime)(config, custom_tools=self._child_tools(spec, context, sources),
                guards=spec.guards, lifecycle=self.runtime.lifecycle)
            contracts = ToolContractBindingsV1('specialist', spec.binding_revision, child_scope,
                evidence_authorizer=EvidenceAuthorizerV1('specialist', self.access.revision,
                    lambda _: self.authorize_result(spec.alias, context, tuple(sources), 'childUse')))
            prepared = child.prepare_turn(artifact, message=arguments['task'],
                input_data={'context': entry['context'], **({'draft': arguments['draft']} if 'draft' in arguments else {})},
                policy=policy, tool_contract_bindings=contracts)
            accounting = context.child_context(invocation_id=child_scope.invocation_id, step_id=child_scope.step_id)
        except (Cancelled, ToolDeadlineExceeded, ValueError, TypeError):
            return unavailable

        def cancelled():
            return (context.cancelled()
                    or not self.authorize_result(spec.alias, context, tuple(sources), 'childUse'))

        if (cancelled() or not self.authorize_launch(spec.alias, arguments, context)
                or not self.authorize_result(spec.alias, context, tuple(sources), 'childUse')):
            return unavailable
        budget = ExecutionBudget(timeout_s=timeout, max_provider_requests=providers,
            max_tool_calls=policy.max_tool_calls, max_reported_tokens=policy.max_reported_tokens,
            cancelled=cancelled)
        self.launches += 1
        with budget.activate():
            result = child.turn(prepared, context=accounting, accounting=self.recorder,
                tool_contract_bindings=contracts, cancelled=cancelled)
        if result.result.exit_code != 0:
            raise ToolEffectFailure('tool_effect_uncertain')
        refs = tuple(source_references(sources))
        try:
            from .accounting_capture import strict_json
            raw = result.result.stdout.encode('utf-8')
            if len(raw) > RESULT_BYTES:
                raise ValueError('specialist result limit')
            finding = strict_json(raw)
            Draft202012Validator(spec.result_schema).validate(finding)
            if b'[cite:' in encode_owned(finding, max_bytes=RESULT_BYTES):
                raise ValueError('child citation cannot appear in the parent finding')
            value = self._project(spec, finding, result, context, refs)
            normalize_tool_result(self.runtime._custom_tools[spec.alias], value)
        except (ValueError, TypeError, ValidationError, RecursionError):
            return ChildResult(ToolFailure('invalid_delegate_output'), refs)
        return ChildResult(value, refs)

    def _project(self, spec, finding, turn, context, refs):
        if spec.select_observations is None:
            return ToolResultV1(data={'finding': finding})
        child = turn.evidence.to_dict()['toolContractV1']['evidence']
        delivered = {row['id']: row for row in child['records'] if row['id'] in child['deliveries']
                     and row.get('originKind') is None}
        selected = spec.select_observations(tuple(dict(id=row['id'], sourceKey=row['sourceKey'],
            sourceRevision=row['sourceRevision'], label=row['label']) for row in delivered.values()))
        if (type(selected) not in (tuple, list) or len(selected) > 64
                or any(type(value) is not str or value not in delivered for value in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError('invalid specialist observation selection')
        calls = {row['callId']: row for row in child['calls']}
        observations, declarations = [], []
        for evidence_id in selected:
            record = delivered[evidence_id]
            body = calls[record['callId']]['outcome']['result']['data']
            value = select_pointer(body, record['selector'])
            if digest(value) != record['contentSha256'] or not self.authorize_result(
                    spec.alias, context, refs, 'observation'):
                raise ValueError('specialist observation unavailable')
            declarations.append(EvidenceDraftV1(record['sourceKey'], record['sourceRevision'],
                record['locator'], f'/observations/{len(observations)}', record['label']))
            observations.append(value)
        return ToolResultV1(data={'finding': finding, 'observations': observations}, evidence=declarations)

    def proof(self):
        return dict(version=1, frameSha256=digest(self.frame.to_dict()), launches=self.launches,
                    providerRequests=self.root.provider_requests, toolCalls=self.root.tool_calls,
                    reportedTokens=self.root.reported_tokens, usageComplete=self.root.usage_complete,
                    receipts=json.loads(encode_owned(self.records)))

    def record(self, context, document):
        if len(self.records) >= self.frame.policy.max_tool_calls:
            raise ToolEffectFailure('tool_journal_failed')
        self.records.append(dict(alias=context.tool_name, callIndex=context.call_index,
                                 childInvocationId=document['childInvocationId'], sources=document['sources']))
        self._exposed.append((context, tuple(document['sources'])))


def child_identity(context):
    return 'child-' + digest([context.scope.operation_namespace, context.scope.invocation_id, context.call_index])


def encode_receipt(execution, context, tool, child):
    outcome = normalize_tool_result(tool, child.value)
    refs = source_references(child.sources)
    encode_owned(refs, max_bytes=PROVENANCE_BYTES)
    document = dict(version=1, frameSha256=digest(execution.frame.to_dict()),
                    childInvocationId=child_identity(context), sources=refs, outcome=outcome)
    return snapshot_outcome({'status': 'ok', 'result': {'subAgentReceiptV1': document}}, JOURNAL_BYTES)


def decode_receipt(execution, context, tool, envelope):
    from .tool_result_contract import validate_tool_result_outcome
    try:
        if envelope['status'] != 'ok' or set(envelope['result']) != {'subAgentReceiptV1'}:
            raise ValueError('invalid child receipt envelope')
        document = envelope['result']['subAgentReceiptV1']
        if (set(document) != {'version', 'frameSha256', 'childInvocationId', 'sources', 'outcome'}
                or type(document['version']) is not int or document['version'] != 1
                or document['frameSha256'] != digest(execution.frame.to_dict())
                or document['childInvocationId'] != child_identity(context)
                or source_references(document['sources']) != document['sources']):
            raise ValueError('invalid child receipt identity')
        encode_owned(document['sources'], max_bytes=PROVENANCE_BYTES)
        return document, validate_tool_result_outcome(tool, document['outcome'])
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ToolEffectFailure('tool_journal_failed') from None
