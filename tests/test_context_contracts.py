"""Context admission rejects unsafe counters and owns serialized configuration."""
from dataclasses import replace
import json
import pytest
import prosaic_runtime as api
from support.context_counter import fixture_context


def exported(name):
    value = getattr(api, name, None)
    assert value is not None, f'public {name} is required'
    return value


@pytest.mark.parametrize('name', ['ContextPolicy', 'TokenCounterDescriptor',
    'TokenCounterRegistration', 'TokenCounterRegistry', 'TokenCounter',
    'ContextAssemblyReport', 'ContextSelection', 'ContextWorkState'])
def test_context_public_exports(name):
    exported(name)


def test_policy_roundtrip_uses_closed_camel_case_contract():
    policy, _ = fixture_context()
    document = policy.to_dict()
    assert document == {'mode': 'fullHistory', 'contextWindowTokens': 4096,
        'reservedOutputTokens': 128, 'safetyMarginTokens': 0,
        'counterIdentity': 'synthetic-byte-v1', 'maxCandidateChecks': 128,
        'maxAssemblyBytes': 33_554_432, 'assemblyTimeoutMs': 2000}
    assert api.ContextPolicy.from_dict(document) == policy
    document['contextWindowTokens'] = 999
    assert policy.context_window_tokens == 4096
    with pytest.raises(api.TurnAdmissionError):
        api.ContextPolicy.from_dict({**policy.to_dict(), 'untrusted': True})


@pytest.mark.parametrize('field,bad', [('mode', 'summarize'),
    ('context_window_tokens', True), ('context_window_tokens', 0),
    ('reserved_output_tokens', 0), ('reserved_output_tokens', 4096),
    ('safety_margin_tokens', -1), ('max_candidate_checks', 1),
    ('max_candidate_checks', 257), ('max_assembly_bytes', 0),
    ('max_assembly_bytes', 268_435_457), ('assembly_timeout_ms', 0),
    ('assembly_timeout_ms', 30_001), ('counter_identity', 'bad\nidentity')])
def test_invalid_policy_cannot_certify_context(field, bad):
    policy, _ = fixture_context()
    with pytest.raises(api.TurnAdmissionError) as caught:
        replace(policy, **{field: bad})
    assert caught.value.code == 'invalidPolicy'


@pytest.mark.parametrize('bad', [True, -1, 1.5, 2**63, None, float('nan')])
def test_counter_result_is_closed(bad):
    policy, registry = fixture_context(count=lambda request: bad)
    from prosaic_runtime.context_counters import count_owned
    with pytest.raises(api.TurnAdmissionError) as caught:
        count_owned(registry.resolve(policy.counter_identity), {'messages': []})
    assert caught.value.code == 'invalidPolicy'


def test_counter_receives_owned_copy_and_exception_text_stays_private():
    def mutate(request):
        request['messages'].append('private-sentinel')
        raise RuntimeError('private-sentinel')
    policy, registry = fixture_context(count=mutate)
    from prosaic_runtime.context_counters import count_owned
    source = {'messages': []}
    with pytest.raises(api.TurnAdmissionError) as caught:
        count_owned(registry.resolve(policy.counter_identity), source)
    assert source == {'messages': []}
    assert str(caught.value) == 'invalidPolicy'
    assert caught.value.__cause__ is None


def test_registry_snapshot_and_descriptor_are_owned():
    policy, registry = fixture_context()
    registration = registry.resolve(policy.counter_identity)
    mapping = {policy.counter_identity: registration}
    owned = api.TokenCounterRegistry(mapping)
    mapping.clear()
    descriptor = owned.resolve(policy.counter_identity).descriptor
    document = descriptor.to_dict()
    document['model'] = 'changed'
    assert descriptor.model == 'fixture-model'
    with pytest.raises(api.TurnAdmissionError):
        owned.resolve('missing')
    with pytest.raises(api.TurnAdmissionError):
        api.TokenCounterRegistry({'wrong-key': registration})


@pytest.mark.parametrize('field,bad', [('provider', 'unknown'),
    ('framing_revision', 'unknown'), ('counting_kind', 'heuristic'),
    ('tokenizer_assets_digest', 'not-a-digest'), ('guarantee', 'x'*17000),
    ('context_capacity_tokens', True)])
def test_descriptor_cannot_claim_unknown_or_unbounded_guarantee(field, bad):
    policy, registry = fixture_context()
    descriptor = registry.resolve(policy.counter_identity).descriptor
    with pytest.raises(api.TurnAdmissionError):
        replace(descriptor, **{field: bad})


def test_work_roundtrip_rejects_unsafe_or_unknown_state():
    work = exported('ContextWorkState').from_dict({'version': 1,
        'candidateChecks': 1, 'candidateBytes': 100, 'activeAssemblyMicros': 5,
        'reservedActiveMicros': 0, 'readmissionCandidateBytes': 100,
        'readmissionPending': True, 'checkpointSequence': 0})
    assert work.to_dict()['candidateBytes'] == 100
    for change in ({'candidateChecks': True}, {'activeAssemblyMicros': -1},
                   {'unexpected': []}, {'version': 2}):
        with pytest.raises(api.TurnAdmissionError):
            api.ContextWorkState.from_dict({**work.to_dict(), **change})


def test_reports_own_ranges_and_enforce_closed_fields():
    report_type = exported('ContextAssemblyReport')
    document = {'version': 1, 'mode': 'omitOldestTurns',
        'counterIdentity': 'synthetic-byte-v1', 'sourceHistoryDigest': 'a'*64,
        'selectedHistoryDigest': 'b'*64, 'inputTokens': 100,
        'countingKind': 'exact', 'reservedOutputTokens': 128,
        'safetyMarginTokens': 0, 'inputCapacityTokens': 3968,
        'encodedInputBytes': 100, 'retainedTurnCount': 1, 'omittedTurnCount': 1,
        'retainedRange': {'startTurn': 2, 'endTurnExclusive': 3},
        'omittedRange': {'startTurn': 1, 'endTurnExclusive': 2},
        'candidateChecks': 2, 'candidateBytes': 400}
    report = report_type.from_dict(document)
    document['retainedRange']['startTurn'] = 99
    assert report.to_dict()['retainedRange']['startTurn'] == 2
    with pytest.raises(api.TurnAdmissionError):
        report_type.from_dict({**report.to_dict(), 'prompt': 'private'})
    assert 'private' not in json.dumps(report.to_dict())
