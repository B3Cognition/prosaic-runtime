"""Typed host completion and bounded provenance; ordinary JSON never grants it."""
from dataclasses import dataclass
import hashlib
import json
import re

from .operation_context import bounded_reference

MAX_BYTES = 8_388_608
MAX_NODES = 65_536
_FIELDS = frozenset({'version', 'invocationId', 'runId', 'stepId', 'toolName',
    'toolVersion', 'toolCallId', 'turn', 'argumentsSha256', 'outputSha256',
    'artifactSha256', 'artifactIncluded'})


class TerminalCompletionError(ValueError):
    """A fixed execution failure, never model correction or effect replay."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def encode(value):
    # Bound traversal before encoding, including wide and cyclic input.
    stack, nodes, text_bytes = [(value, 0)], 0, 0
    while stack:
        child, level = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or level > 64:
            raise ValueError('terminal JSON limit')
        if type(child) is dict:
            if any(type(k) is not str for k in child):
                raise ValueError('terminal JSON keys')
            if nodes + len(stack) + len(child) > MAX_NODES:
                raise ValueError('terminal JSON limit')
            text_bytes += sum(len(key.encode('utf-8')) for key in child)
            stack.extend((v, level + 1) for v in child.values())
        elif type(child) is list:
            if nodes + len(stack) + len(child) > MAX_NODES:
                raise ValueError('terminal JSON limit')
            stack.extend((v, level + 1) for v in child)
        elif child is not None and type(child) not in (str, bool, int, float):
            raise ValueError('terminal JSON value')
        elif type(child) is str:
            text_bytes += len(child.encode('utf-8'))
        elif type(child) is int:
            # Decimal digits are at least floor(log10(2) * bit_length).
            # A conservative lower bound prevents repeated huge integer aliases
            # from reaching the encoder; the exact encoded cap is checked below.
            text_bytes += max(1, child.bit_length() * 3 // 10)
        if text_bytes > MAX_BYTES:
            raise ValueError('terminal JSON limit')
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False)
        if len(raw.encode('utf-8')) > MAX_BYTES:
            raise ValueError('terminal JSON limit')
        return raw
    except (UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError('terminal JSON value') from exc


def content_digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True, init=False)
class TerminalCompletion:
    """A trusted callback's output plus its actual validated inline artifact.

    ALWAYS validate domain completeness before constructing this value.
    NEVER use acknowledgement text as a substitute for the product artifact.
    """
    _encoded: str

    def __init__(self, output, *, artifact):
        object.__setattr__(self, '_encoded', encode({'output': output, 'artifact': artifact}))

    @property
    def output(self):
        return json.loads(self._encoded)['output']

    @property
    def artifact(self):
        return json.loads(self._encoded)['artifact']


def admit_terminal_tools(tools, names, grants=(), *, supported=True):
    """Pure admission, before callbacks, journal claims or provider requests."""
    effective = frozenset(name for name in names if name in tools and tools[name].terminal)
    grants = frozenset(grants)
    if any(name not in tools or not tools[name].terminal for name in grants):
        raise ValueError('invalid terminal tool grant')
    if effective and (not supported or effective - grants):
        raise ValueError('terminal tool completion is not granted or supported')
    if any(tools[name].operation_key is not None for name in effective):
        raise ValueError('journaled terminal tools are unsupported')
    if any(not bounded_reference(tools[name].version) for name in effective):
        raise ValueError('invalid terminal tool version')
    return effective & grants


def binding(evidence):
    return {key: value for key, value in evidence.items() if key != 'artifact'}


def validate_evidence(evidence, *, output=None, check_output=False, complete=True):
    """Validate a private receipt document, not a model-supplied envelope."""
    if type(evidence) is not dict or set(evidence) != _FIELDS | ({'artifact'} if complete else set()):
        raise ValueError('invalid terminal evidence')
    if type(evidence['version']) is not int or evidence['version'] != 1:
        raise ValueError('invalid terminal evidence version')
    if type(evidence['artifactIncluded']) is not bool or evidence['artifactIncluded'] != complete:
        raise ValueError('incomplete terminal artifact')
    for name in ('invocationId', 'toolName', 'toolVersion', 'toolCallId'):
        if not bounded_reference(evidence[name]):
            raise ValueError('invalid terminal identity')
    for name in ('runId', 'stepId'):
        if evidence[name] is not None and not bounded_reference(evidence[name]):
            raise ValueError('invalid terminal scope')
    if type(evidence['turn']) is not int or evidence['turn'] < 1:
        raise ValueError('invalid terminal turn')
    for name in ('argumentsSha256', 'outputSha256', 'artifactSha256'):
        if type(evidence[name]) is not str or re.fullmatch('[0-9a-f]{64}', evidence[name]) is None:
            raise ValueError('invalid terminal digest')
    if complete and content_digest(evidence['artifact']) != evidence['artifactSha256']:
        raise ValueError('terminal artifact mismatch')
    if check_output and content_digest(output) != evidence['outputSha256']:
        raise ValueError('terminal output mismatch')
    return json.loads(encode(evidence))


class TerminalCapture:
    """Private per-invocation latch, populated before post-handler boundaries."""
    def __init__(self):
        self.evidence = None

    def capture(self, value, *, tool, arguments, scope, call_id, turn):
        if self.evidence is not None or type(value) is not TerminalCompletion:
            raise TerminalCompletionError('terminal_completion_invalid')
        evidence = {'version': 1, 'invocationId': scope.invocation_id,
            'runId': scope.run_id, 'stepId': scope.step_id,
            'toolName': tool.name, 'toolVersion': tool.version, 'toolCallId': call_id,
            'turn': turn, 'argumentsSha256': content_digest(arguments),
            'outputSha256': content_digest(value.output),
            'artifactSha256': content_digest(value.artifact), 'artifactIncluded': True,
            'artifact': value.artifact}
        breadcrumb = binding(evidence)
        breadcrumb['artifactIncluded'] = False
        self.evidence = validate_evidence(breadcrumb, complete=False)
        try:
            too_large = len(encode({'output': value.output, 'terminalCompletion': evidence}).encode()) > tool.max_result_bytes
        except ValueError:
            too_large = True
        if too_large:
            raise TerminalCompletionError('terminal_completion_limit')
        self.evidence = validate_evidence(evidence, output=value.output, check_output=True)
