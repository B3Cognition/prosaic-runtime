"""Immutable bytes captured using published 0.9.17, outside the source tree."""
import hashlib
import json
from pathlib import Path

from prosaic_runtime import ProsaicRuntime, RuntimeConfig, EndpointConfig, ProsaicArtifact, RunPolicy

FIXTURE = Path(__file__).parent / 'fixtures/frozen_turns/legacy/released.json'


def test_released_legacy_fixtures_load_unchanged():
    content = FIXTURE.read_bytes()
    assert hashlib.sha256(content).hexdigest() == 'f6b0e4bd31fbf529c8766a6bbb074a39799b04fcc7bcfc1765d6b0c75192d5a1'
    fixture = json.loads(content)
    runtime = ProsaicRuntime(RuntimeConfig({'local': EndpointConfig(
        'http://127.0.0.1:9/v1', 'synthetic', features={'streaming': False})}, {}, 'local', frozenset()))
    artifact = ProsaicArtifact('approved', 'subagent', {'name': 'approved', 'description': 'Synthetic'},
        'ALWAYS use supplied facts. NEVER invent evidence.')
    prepared = runtime.prepare_turn(artifact, message='Hello', input_data={'task': 'Review ž'},
        policy=RunPolicy(max_provider_requests=2, max_tool_calls=0))
    expected = fixture['preparations']['1']
    assert prepared.context_identity == expected['contextIdentity']
    assert prepared.encoding_version == 'conversation-turn-v1'
    assert json.loads(prepared._encoded) == expected['document']
    # Descriptor wording is identity material; reproduce the released registration exactly.
    import prosaic_runtime as api
    class Counter:
        def count_tokens(self, request): return 10
    descriptor = api.TokenCounterDescriptor('synthetic-byte-v1', 'openai-compatible', 'synthetic',
        'openai-chat-v1', 1048576, '1', '0'*64, '1'*64, 'exact', 'Synthetic fixture only.')
    registry = api.TokenCounterRegistry({'synthetic-byte-v1': api.TokenCounterRegistration(descriptor, Counter())})
    context = api.ContextPolicy('fullHistory', 4096, 128, 0, 'synthetic-byte-v1', 128, 33554432, 2000)
    second = runtime.prepare_turn(artifact, message='Hello', input_data={'task': 'Review ž'},
        policy=RunPolicy(max_provider_requests=2, max_tool_calls=0), context_policy=context, token_counters=registry)
    assert second.context_identity == fixture['preparations']['2']['contextIdentity']
    assert json.loads(second._encoded) == fixture['preparations']['2']['document']


def test_released_rule_fingerprint_is_unchanged():
    import prosaic_runtime as api
    content=(FIXTURE.parent/'rules.json').read_bytes()
    assert hashlib.sha256(content).hexdigest()=='c1f803dbef9880f10d1b37b378d60a96f56adfeacbbdeaf74f085ad24241f9bf'
    expected=json.loads(content)
    runtime=api.ProsaicRuntime(api.RuntimeConfig({'local':api.EndpointConfig(
        'http://127.0.0.1:9/v1','synthetic',features={'streaming':False})},{},'local',frozenset()),guards=api.generic_guards())
    agent=api.ProsaicArtifact('approved','subagent',{},'- ALWAYS use plain links. <!-- enforce: no_wrapped_links(stage=final, on_violation=fix) -->')
    prepared=runtime.prepare_turn(agent,message='Hello')
    assert prepared.context_identity==expected['contextIdentity']
    assert json.loads(prepared._encoded)['rules']==expected['rules']
