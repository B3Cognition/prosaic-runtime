"""Closed, deterministic host assertions; never provider attestations."""
from contextvars import ContextVar
import hashlib
import json
import math
import re

_CASES = ('text_complete', 'stream_terminal', 'required_tool', 'arguments_validated',
          'strict_json', 'usage_complete', 'cancellation_boundary')
_SUITES = {'text-tools-v1': _CASES}  # Every case is required in this version.
_COUNTS = frozenset({'providerRequests', 'toolCalls', 'reportedTokens', 'handlerCalls'})
_DIGESTS = frozenset({'artifactSha256', 'argumentsSha256', 'outputSha256'})
_REASONS = {
    'passed': frozenset({'complete', 'invalid_arguments', 'cancelled_before_followup'}),
    'failed': frozenset({'incomplete', 'invalid_arguments', 'invalid_output'}),
    'unsupported': frozenset({'not_requested', 'unsupported_feature'}),
    'unknown': frozenset({'incomplete', 'usage_unknown'}),
    'not_run': frozenset({'not_executed'}),
}
_MAX_BYTES = 65536
_TOOL_NAME = 'conformance_lookup'
_tool_evidence = ContextVar('prosaic_conformance_tool_evidence', default=None)
_suite_deadline = ContextVar('prosaic_conformance_suite_deadline', default=None)


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _canonical(value):
    """Bound traversal before encoding, including cycles and giant integers."""
    stack = [(value, 0, frozenset())]
    size = 0
    while stack:
        child, level, ancestors = stack.pop()
        if type(child) in (dict, list):
            level += 1
            if level > 16 or id(child) in ancestors:
                raise ValueError('conformance nesting or cycle')
            if len(child) > _MAX_BYTES:
                raise ValueError('conformance size limit')
            ancestors = ancestors | {id(child)}
            if type(child) is dict:
                if any(type(k) is not str for k in child):
                    raise ValueError('conformance keys must be strings')
                stack.extend((k, level, ancestors) for k in child)
                children = child.values()
            else:
                children = child
            stack.extend((v, level, ancestors) for v in children)
            size += len(child)
        elif type(child) is str:
            try:
                size += len(child.encode('utf-8'))
            except UnicodeError:
                raise ValueError('invalid conformance text') from None
        elif type(child) is int:
            if abs(child) > 2**63 - 1:
                raise ValueError('conformance integer limit')
            size += 20
        elif type(child) is float:
            if not math.isfinite(child):
                raise ValueError('nonfinite conformance value')
            size += 24
        elif child is None or type(child) is bool:
            size += 5
        else:
            raise ValueError('conformance must be finite JSON')
        if size > _MAX_BYTES:
            raise ValueError('conformance size limit')
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > _MAX_BYTES:
        raise ValueError('conformance size limit')
    return encoded


def evaluate_conformance(suite: str, profile_fingerprint: str, observations: dict) -> dict:
    """Validate imported host assertions and return an owned canonical report."""
    if type(suite) is not str or suite not in _SUITES:
        raise ValueError('unknown conformance suite')
    if not _digest(profile_fingerprint):
        raise ValueError('invalid profile fingerprint')
    _canonical(observations)
    if type(observations) is not dict or set(observations) - set(_SUITES[suite]):
        raise ValueError('unknown conformance case')
    cases = []
    required = {'state', 'reason', 'suiteId', 'profileFingerprint', 'origin'}
    for name in _SUITES[suite]:
        if name not in observations:
            cases.append({'id': name, 'state': 'not_run', 'reason': 'not_executed',
                          'origin': 'unexecuted'})
            continue
        record = observations[name]
        if type(record) is not dict or not required <= record.keys() or set(record) - required - _COUNTS - _DIGESTS:
            raise ValueError('invalid conformance assertion fields')
        state, reason, origin = record['state'], record['reason'], record['origin']
        if (type(state) is not str or state not in _REASONS or type(reason) is not str or
                reason not in _REASONS[state] or type(origin) is not str or origin not in {'fixture', 'live'}):
            raise ValueError('invalid conformance assertion state')
        if record['suiteId'] != suite or record['profileFingerprint'] != profile_fingerprint:
            raise ValueError('conformance evidence binding mismatch')
        if state == 'passed':
            want = ('invalid_arguments' if name == 'arguments_validated' else
                    'cancelled_before_followup' if name == 'cancellation_boundary' else 'complete')
            if reason != want:
                raise ValueError('invalid conformance success reason')
        if any(not _digest(record[k]) for k in _DIGESTS & record.keys()):
            raise ValueError('invalid conformance digest')
        if any(type(record[k]) is not int or not 0 <= record[k] <= 2**63 - 1
               for k in _COUNTS & record.keys()):
            raise ValueError('invalid conformance count')
        cases.append({'id': name, **{k: record[k] for k in record if k not in {'suiteId', 'profileFingerprint'}}})
    origins = {case['origin'] for case in cases}
    qualification = 'not_qualified'
    if all(case['state'] == 'passed' for case in cases):
        if origins == {'live'}:
            qualification = 'qualified'
        elif origins == {'fixture'}:
            qualification = 'fixture_passed'
    from . import __version__
    report = {'version': 1, 'suiteId': suite, 'runtimeVersion': __version__,
              'profileFingerprint': profile_fingerprint, 'cases': cases,
              'qualification': qualification}
    _canonical(report)
    return report


def _profile_fingerprint(endpoint):
    # Credentials and credential references never enter the identity payload.
    identity = {'version': 1, 'provider': endpoint.provider, 'model': endpoint.model,
                'baseUrl': endpoint.base_url, 'features': endpoint.features,
                'temperature': endpoint.temperature, 'maxTokens': endpoint.max_tokens,
                'maxResponseBytes': endpoint.max_response_bytes}
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _record_tool_result(name, payload):
    """Private synchronous evidence tap. Ordinary execution retains no record."""
    collector = _tool_evidence.get()
    if collector is None:
        return
    if name != _TOOL_NAME:
        collector['other'] += 1
    elif payload.get('status') == 'ok':
        collector['ok'] += 1
    elif payload.get('status') == 'error' and payload.get('error') == 'invalid_arguments':
        collector['invalid'] += 1
    else:
        collector['other'] += 1


def _record_stream_terminal():
    collector = _tool_evidence.get()
    if collector is not None:
        collector['terminal'] = True


def _check_suite_deadline():
    """Conformance-only effect guard; ordinary invocations keep their contract."""
    deadline = _suite_deadline.get()
    if deadline is not None:
        import time
        from .tools import ToolDeadlineExceeded
        if time.monotonic() >= deadline:
            raise ToolDeadlineExceeded('conformance suite deadline exceeded')


def _strict_output(text):
    from .tools import unique_pairs, reject_constant
    try:
        if len(text.encode('utf-8')) > _MAX_BYTES:
            return False
        value = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        _canonical(value)
        return type(value) is dict and set(value) == {'value'} and type(value['value']) is int and value['value'] == 7
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return False


def _run_suite(config, profile, fingerprint, policy, observer, origin):
    """Execute synthetic scenarios with one absolute deadline and shared counts."""
    from dataclasses import replace
    from pathlib import Path
    import tempfile
    import time
    from . import CustomTool, ProsaicArtifact, ProsaicRuntime, RunPolicy, RuntimeConfig
    from .openai_compatible import _feature_enabled

    if type(origin) is not str or origin not in {'fixture', 'live'}:
        raise ValueError('invalid conformance evidence origin')
    if observer is not None and not callable(observer):
        raise ValueError('observer must be callable')
    if policy is None:
        policy = RunPolicy(timeout_s=60, max_provider_requests=12, max_tool_calls=4,
                           max_reported_tokens=32768, max_input_bytes=_MAX_BYTES, max_tool_rounds=2)
    if type(policy) is not RunPolicy:
        raise ValueError('conformance policy must be RunPolicy')
    RunPolicy.__post_init__(policy)
    for name in ('max_provider_requests', 'max_tool_calls', 'max_reported_tokens'):
        value = getattr(policy, name)
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError('conformance requires finite shared allowances')

    endpoint = config.profiles[profile]
    streaming = _feature_enabled(endpoint.features, 'streaming', default=True)
    json_enabled = _feature_enabled(endpoint.features, 'json_mode', default=False)
    json_off = 'json_mode' in endpoint.features and not json_enabled
    # Never discover configured CLI tools or enable arbitrary transcript/file/web
    # features. This suite exercises only the provider request controls below.
    features = {key: value for key, value in endpoint.features.items()
                if key in {'streaming', 'stream_options', 'json_mode', 'reasoning_effort', 'effort', 'thinking'}}
    isolated_endpoint = replace(endpoint, features=features,
                                max_response_bytes=min(_MAX_BYTES, endpoint.max_response_bytes))
    isolated = RuntimeConfig({profile: isolated_endpoint}, {}, profile, frozenset({_TOOL_NAME}))
    deadline = time.monotonic() + policy.timeout_s
    totals = {'provider_requests': 0, 'tool_calls': 0, 'reported_tokens': 0}
    observations = {}
    def assertion(name, state, reason, **evidence):
        observations[name] = {'state': state, 'reason': reason, 'suiteId': 'text-tools-v1',
            'profileFingerprint': fingerprint, 'origin': origin, **evidence}
    if not streaming:
        assertion('stream_terminal', 'unsupported', 'not_requested')
    if json_off:
        assertion('strict_json', 'unsupported', 'not_requested')
    prompts = {
        'text_complete': 'Return the plain text complete.',
        'stream_terminal': 'Return the plain text complete in a completed stream.',
        'required_tool': 'First invoke conformance_lookup with {"value":7}, then return complete.',
        'arguments_validated': 'First invoke conformance_lookup with the deliberately invalid arguments '
                               '{"value":"invalid"}. After the rejection return complete. Do not retry.',
        'strict_json': 'Return only the JSON object {"value":7}, with no other keys or prose.',
        'usage_complete': 'Return the plain text complete.',
        'cancellation_boundary': 'First invoke conformance_lookup with {"value":7}, then return complete.',
    }
    with tempfile.TemporaryDirectory(prefix='prosaic-conformance-') as directory:
        for name in _CASES:
            if name in observations:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or any(totals[k] >= getattr(policy, 'max_' + k) for k in totals):
                break
            calls = 0
            cancelled = False
            collector = {'ok': 0, 'invalid': 0, 'other': 0, 'terminal': False}
            def lookup(arguments):
                nonlocal calls, cancelled
                calls += 1
                if name == 'cancellation_boundary':
                    cancelled = True
                return {'value': 7}
            tool = CustomTool(_TOOL_NAME, 'Read the immutable synthetic value seven.',
                {'type': 'object', 'properties': {'value': {'type': 'integer', 'enum': [7]}},
                 'required': ['value'], 'additionalProperties': False}, lookup, 'conformance-v1',
                max_argument_bytes=1024, max_result_bytes=1024)
            with_tools = name in {'required_tool', 'arguments_validated', 'cancellation_boundary'}
            artifact = ProsaicArtifact.from_inspection({'id': 'conformance/' + name,
                'type': 'subagent', 'frontmatter': {'tools': [_TOOL_NAME] if with_tools else []},
                'body': 'Conformance case ' + name + '. ' + prompts[name]})
            allowance = replace(policy, allowed_tools=frozenset({_TOOL_NAME}) if with_tools else frozenset(),
                read_roots=(), write_paths=(), forbidden_roots=(), timeout_s=remaining,
                max_input_bytes=min(policy.max_input_bytes, _MAX_BYTES),
                initial_tool=_TOOL_NAME if with_tools else None,
                max_provider_requests=policy.max_provider_requests - totals['provider_requests'],
                max_tool_calls=policy.max_tool_calls - totals['tool_calls'],
                max_reported_tokens=policy.max_reported_tokens - totals['reported_tokens'])
            runtime = ProsaicRuntime(isolated, source=Path(directory), custom_tools={_TOOL_NAME: tool})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            allowance = replace(allowance, timeout_s=remaining)
            token = _tool_evidence.set(collector)
            deadline_token = _suite_deadline.set(deadline)
            try:
                result = runtime.run(artifact, cwd=directory, policy=allowance,
                    cancelled=lambda: cancelled, observer=observer)
            finally:
                _suite_deadline.reset(deadline_token)
                _tool_evidence.reset(token)
            counts = result.metadata['invocation_budgets_v1']
            for key in totals:
                totals[key] += counts[key]
            measured = {'artifactSha256': artifact.digest,
                        'outputSha256': hashlib.sha256(result.stdout.encode('utf-8')).hexdigest(),
                        'providerRequests': counts['provider_requests'], 'toolCalls': counts['tool_calls'],
                        'reportedTokens': counts['reported_tokens'], 'handlerCalls': calls}
            complete = (result.exit_code == 0 and result.metadata.get('finish_reason') == 'stop'
                        and bool(result.stdout.strip()) and len(result.stdout.encode('utf-8')) <= _MAX_BYTES)
            passed, reason = complete, 'complete'
            if name == 'stream_terminal':
                passed = complete and result.metadata.get('streamed') is True
                if endpoint.provider == 'openai-compatible':
                    passed = passed and collector['terminal']
            elif name == 'required_tool':
                passed = complete and calls == 1 and collector['ok'] == 1 and collector['invalid'] == collector['other'] == 0
            elif name == 'arguments_validated':
                passed = complete and calls == 0 and collector['invalid'] == 1 and collector['ok'] == collector['other'] == 0
                reason = 'invalid_arguments'
            elif name == 'strict_json':
                passed = complete and _strict_output(result.stdout)
                if not passed:
                    reason = 'invalid_output'
            elif name == 'usage_complete':
                passed = counts['provider_requests'] > 0 and counts['usage_complete'] and result.token_usage is not None
            elif name == 'cancellation_boundary':
                passed = (calls == 1 and cancelled and result.exit_code == 130 and
                          result.metadata.get('failure_reason') == 'cancelled' and counts['provider_requests'] == 1)
                reason = 'cancelled_before_followup' if passed else 'incomplete'
            assertion(name, 'passed' if passed else 'failed',
                      reason if passed or reason in {'invalid_arguments', 'invalid_output'} else 'incomplete', **measured)
            if not counts['usage_complete'] or (counts['provider_requests'] and result.token_usage is None):
                assertion('usage_complete', 'unknown', 'usage_unknown')
                break
    return evaluate_conformance('text-tools-v1', fingerprint, observations)
