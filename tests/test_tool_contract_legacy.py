import hashlib
import json
from pathlib import Path
import prosaic_runtime as api

ROOT = Path(__file__).parent / 'fixtures/tool_contracts/legacy'


def test_absent_tool_contract_is_byte_identical():
    from prosaic_runtime.tools import bounded_result
    from prosaic_runtime.events import event_context, emit
    from prosaic_runtime.conversation_preparation import policy_document
    raw = (ROOT / 'released.json').read_bytes()
    provenance = json.loads((ROOT / 'provenance.json').read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance['fixtureSha256']
    old = json.loads(raw)
    tool = api.CustomTool('lookup', 'Lookup', {'type': 'object', 'properties': {}, 'additionalProperties': False}, lambda a: {}, 'v1')
    assert tool.descriptor == old['descriptor']
    policy = api.RunPolicy(max_provider_requests=2, max_tool_calls=0)
    assert policy_document(policy) == old['policy']
    assert bounded_result({'count': 14}, 65536) == old['success']
    assert bounded_result(api.ToolFailure('missing'), 65536) == old['failure']
    events = []
    with event_context(events.append):
        emit('tool_started', name='lookup', call_id='call-1', turn=1)
        emit('tool_completed', name='lookup', call_id='call-1', turn=1, status='ok', duration_ms=42)
    assert events == old['events']
    runtime = api.ProsaicRuntime(api.RuntimeConfig({'local': api.EndpointConfig('http://127.0.0.1:9/v1', 'synthetic', features={'streaming': False})}, {}, 'local', frozenset()))
    agent = api.ProsaicArtifact('approved', 'subagent', {'name': 'approved', 'description': 'Synthetic'}, 'ALWAYS use supplied facts. NEVER invent evidence.')
    prepared = runtime.prepare_turn(agent, message='Hello', input_data={'task': 'Review ž'}, policy=policy)
    assert prepared.context_identity == old['prepared']['identity']
    assert prepared.encoding_version == old['prepared']['encoding']
    assert json.loads(prepared._encoded) == old['prepared']['document']
