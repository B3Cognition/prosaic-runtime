"""Host-owned bindings for one level of serial agent delegation."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Callable

from .artifacts import ProsaicArtifact
from .conversation import encode_owned
from .mcp_catalog import admitted_schema
from .operation_context import InvocationScope, bounded_reference
from .policy import RunPolicy, requested_tools
from .tool_contracts import ToolContractBindingsV1, ToolEventPolicyV1, ToolResultContractV1
from .tools import CustomTool, validate_custom_tools

TASK_BYTES = 8192
RESULT_BYTES = 16384
FRAME_BYTES = 65536
PROVENANCE_BYTES = 16384
MAX_SOURCES = 64


def digest(document):
    return sha256(encode_owned(document)).hexdigest()


def source_references(values):
    if type(values) not in (list, tuple) or len(values) > MAX_SOURCES:
        raise ValueError('invalid delegation sources')
    if any(not bounded_reference(value, 256) for value in values):
        raise ValueError('invalid delegation source reference')
    return sorted(set(values))


@dataclass(frozen=True)
class SubAgentPolicyV1:
    max_delegations: int
    max_provider_requests: int
    max_tool_calls: int
    max_reported_tokens: int

    def __post_init__(self):
        if type(self.max_delegations) is not int or not 1 <= self.max_delegations <= 8:
            raise ValueError('invalid delegation allowance')
        for name in ('max_provider_requests', 'max_tool_calls', 'max_reported_tokens'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError('invalid shared allowance')


@dataclass(frozen=True, init=False)
class SubAgentFrameV1:
    scope: InvocationScope
    policy: SubAgentPolicyV1
    access_revision: str
    _encoded: bytes = field(repr=False)

    def __init__(self, scope, policy, access_revision, projections):
        if (type(scope) is not InvocationScope or scope.operation_namespace is None
                or type(policy) is not SubAgentPolicyV1 or not bounded_reference(access_revision)
                or type(projections) is not dict or not 1 <= len(projections) <= 8):
            raise ValueError('invalid delegation frame')
        owned = json.loads(encode_owned(projections, max_bytes=FRAME_BYTES))
        for name, entry in owned.items():
            if (not bounded_reference(name) or type(entry) is not dict or set(entry) != {'context', 'sources'}
                    or type(entry['context']) is not dict):
                raise ValueError('invalid child projection')
            entry['sources'] = source_references(entry['sources'])
        object.__setattr__(self, 'scope', scope)
        object.__setattr__(self, 'policy', policy)
        object.__setattr__(self, 'access_revision', access_revision)
        object.__setattr__(self, '_encoded', encode_owned(owned, max_bytes=FRAME_BYTES))

    @property
    def identity(self):
        return digest(self.to_dict())

    @property
    def projections(self):
        return json.loads(self._encoded)

    def to_dict(self):
        return dict(version=1, scope=asdict(self.scope), policy=asdict(self.policy),
                    accessRevision=self.access_revision, projections=self.projections)

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict
                or set(value) != {'version', 'scope', 'policy', 'accessRevision', 'projections'}
                or type(value['version']) is not int or value['version'] != 1):
            raise ValueError('invalid delegation frame')
        return cls(InvocationScope(**value['scope']), SubAgentPolicyV1(**value['policy']),
                   value['accessRevision'], value['projections'])


@dataclass(frozen=True)
class SubAgentAccessV1:
    revision: str
    authorize_launch: Callable
    authorize_result: Callable
    check_owner: Callable

    def __post_init__(self):
        if not bounded_reference(self.revision) or not all(callable(callback) for callback in
                (self.authorize_launch, self.authorize_result, self.check_owner)):
            raise ValueError('invalid delegation access binding')


@dataclass(frozen=True)
class SubAgentReadV1:
    """A reviewed local read and the trusted footprint of its returned data."""
    tool: CustomTool
    source_refs: Callable | None = None
    public: bool = False

    def __post_init__(self):
        from .mcp import is_mcp_tool
        if (type(self.tool) is not CustomTool or self.tool.terminal or self.tool.operation_key is not None
                or is_mcp_tool(self.tool) or is_sub_agent_tool(self.tool)
                or type(self.tool.result_contract) is not ToolResultContractV1
                or type(self.public) is not bool
                or (self.source_refs is not None and not callable(self.source_refs))
                or self.public == (self.source_refs is not None)):
            raise ValueError('delegate reads require a reviewed local typed read and its source policy')


@dataclass(frozen=True, init=False)
class SubAgentSpecV1:
    alias: str
    description: str
    binding_revision: str
    policy: RunPolicy
    reads: tuple
    event_policy: ToolEventPolicyV1 | None
    guards: tuple
    select_observations: Callable | None
    _artifact: bytes = field(repr=False)
    _result_schema: bytes = field(repr=False)
    _draft_schema: bytes | None = field(repr=False)

    def __init__(self, alias, description, artifact, binding_revision, policy, result_schema, *,
                 reads=(), draft_schema=None, event_policy=None, guards=(), select_observations=None):
        from .rules import validate_guards
        if type(artifact) is not ProsaicArtifact or not bounded_reference(binding_revision):
            raise ValueError('invalid specialist registration')
        reads = tuple(reads)
        if any(type(item) is not SubAgentReadV1 for item in reads):
            raise ValueError('unreviewed specialist tool')
        names = frozenset(item.tool.name for item in reads)
        if (type(policy) is not RunPolicy or names != policy.allowed_tools
                or len(names) != len(reads) or requested_tools(artifact.frontmatter.get('tools')) != names
                or policy.initial_tool is not None or policy.parallel_tools is not None
                or policy.allowed_terminal_tools or policy.read_roots or policy.write_paths or policy.forbidden_roots
                or not policy.max_provider_requests or policy.max_tool_calls is None
                or not policy.max_reported_tokens):
            raise ValueError('invalid child execution policy')
        if select_observations is not None and not callable(select_observations):
            raise ValueError('invalid observation selector')
        if event_policy is not None and type(event_policy) is not ToolEventPolicyV1:
            raise ValueError('invalid delegation event policy')
        values = dict(alias=alias, description=description, binding_revision=binding_revision,
                      policy=policy, reads=reads, event_policy=event_policy, select_observations=select_observations,
                      guards=tuple(validate_guards(guards).values()),
                      _artifact=encode_owned(artifact.inspection(), max_bytes=262144),
                      _result_schema=encode_owned(admitted_schema(result_schema, RESULT_BYTES, input_schema=True, closed=True)),
                      _draft_schema=None if draft_schema is None else
                          encode_owned(admitted_schema(draft_schema, TASK_BYTES, input_schema=True, closed=True)))
        for key, value in values.items():
            object.__setattr__(self, key, value)

    @property
    def artifact(self):
        return ProsaicArtifact.from_inspection(json.loads(self._artifact))

    @property
    def result_schema(self):
        return json.loads(self._result_schema)

    def registration(self):
        from .conversation_preparation import policy_document
        properties = {'task': {'type': 'string', 'minLength': 1}}
        if self._draft_schema is not None:
            properties['draft'] = json.loads(self._draft_schema)
        parameters = dict(type='object', properties=properties, required=['task'], additionalProperties=False)
        schema = dict(type='object', properties={'finding': self.result_schema,
                      'observations': {'type': 'array'}}, required=['finding'], additionalProperties=False)
        result = ToolResultContractV1('sub-agent-result-v1', schema)
        version = 'sub-agent-v1.' + digest({
            'revision': self.binding_revision, 'artifact': json.loads(self._artifact),
            'policy': policy_document(self.policy), 'arguments': parameters, 'result': result.to_dict(),
            'reads': [{'tool': item.tool.descriptor, 'public': item.public} for item in self.reads],
            'guards': [[guard.name, guard.version] for guard in self.guards],
            'selectsObservations': self.select_observations is not None,
            'events': None if self.event_policy is None else self.event_policy.to_dict(),
        })
        handler = _Delegate(self)
        return CustomTool(self.alias, self.description, parameters, handler, version, with_context=True,
                          authorize=handler.authorize, operation_key=_operation_key,
                          max_argument_bytes=TASK_BYTES, max_result_bytes=RESULT_BYTES,
                          result_contract=result, event_policy=self.event_policy)


def _operation_key(_arguments, context):
    return 'sub-agent-v1.' + digest([context.scope.invocation_id, context.call_index])


@dataclass(frozen=True)
class _Delegate:
    spec: SubAgentSpecV1

    def authorize(self, arguments, context):
        from .sub_agent_execution import current_execution
        execution = current_execution()
        return execution is not None and execution.authorize_launch(self.spec.alias, arguments, context)

    def __call__(self, arguments, context):
        from .sub_agent_execution import current_execution
        return current_execution().invoke(self.spec, arguments, context)


def is_sub_agent_tool(tool):
    return type(tool) is CustomTool and type(tool.handler) is _Delegate


def bind_sub_agent_tools(specs, *, existing=None):
    registry = validate_custom_tools(existing)
    for spec in specs:
        if type(spec) is not SubAgentSpecV1 or spec.alias in registry:
            raise ValueError('invalid or duplicate specialist alias')
        registry[spec.alias] = spec.registration()
    return registry


def selected_sub_agents(tools, selected):
    return {name: tools[name] for name in selected if is_sub_agent_tool(tools.get(name))}


def admit_sub_agents(runtime, artifact, selected, policy, contracts, frame):
    tools = selected_sub_agents(runtime._custom_tools, selected)
    if not tools and frame is None:
        return
    if (not tools or type(frame) is not SubAgentFrameV1 or type(contracts) is not ToolContractBindingsV1
            or frame.scope != contracts.scope or set(frame.projections) != tools.keys()
            or policy.initial_tool in tools or runtime.config.tool_directories
            or any(getattr(policy, name) is None or getattr(frame.policy, name) > getattr(policy, name)
                   for name in ('max_provider_requests', 'max_tool_calls', 'max_reported_tokens'))):
        raise ValueError('invalid delegated turn bindings')
    from .conversation_preparation import turn_endpoint
    profile, endpoint, _ = turn_endpoint(runtime, artifact, policy)
    if endpoint.provider not in ('openai-compatible', 'anthropic'):
        raise ValueError('unsupported delegation provider')
    for tool in tools.values():
        spec = tool.handler.spec
        expected = spec.registration()
        if (tool.descriptor != expected.descriptor or tool.operation_key is not _operation_key
                or tool.authorize != tool.handler.authorize):
            raise ValueError('delegate registration drift')
        child_profile, _, _ = turn_endpoint(runtime, spec.artifact, spec.policy)
        if child_profile != profile:
            raise ValueError('specialist route differs from the parent')
        from dataclasses import replace
        from .rules import turn_rules
        tier = spec.artifact.frontmatter.get('model_tier')
        child_config = replace(runtime.config, default_profile=profile, profiles={profile: endpoint},
            allowed_tools=spec.policy.allowed_tools, routes={tier: profile} if tier is not None else {},
            required_guards={})
        child = type(runtime)(child_config, custom_tools={read.tool.name: read.tool for read in spec.reads},
                              guards=spec.guards)
        rules = turn_rules(child, spec.artifact, warn=False)
        if any(binding.paths for binding in rules.bindings):
            raise ValueError('specialist guards cannot read host paths')
