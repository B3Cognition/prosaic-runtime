"""Installed SDK-only consumer for safe events, evidence, citations and frozen turns."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
import json
from pathlib import Path
import re
from threading import Thread
import prosaic_runtime as api


def exercise(provider):
    requests, callbacks, events = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            requests.append(raw)
            if len(requests) == 1:
                if provider == 'anthropic':
                    answer = dict(type='message', id='private', role='assistant', model='fixture',
                        content=[dict(type='tool_use', id='provider-private', name='lookup', input={})],
                        stop_reason='tool_use', usage=dict(input_tokens=2, output_tokens=3))
                else:
                    answer = dict(choices=[dict(message=dict(role='assistant', content='', tool_calls=[dict(
                        id='provider-private', type='function', function=dict(name='lookup', arguments='{}'))]),
                        finish_reason='tool_calls')], usage=dict(prompt_tokens=2, completion_tokens=3, total_tokens=5))
            else:
                evidence_id = re.search(rb'ev_[0-9a-f]{64}', raw).group().decode()
                text = f'14 dependents [cite:{evidence_id}]'
                if provider == 'anthropic':
                    answer = dict(type='message', id='private', role='assistant', model='fixture',
                        content=[dict(type='text', text=text)], stop_reason='end_turn', usage=dict(input_tokens=2, output_tokens=3))
                else:
                    answer = dict(choices=[dict(message=dict(role='assistant', content=text), finish_reason='stop')],
                        usage=dict(prompt_tokens=2, completion_tokens=3, total_tokens=5))
            body = json.dumps(answer).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        def handler(arguments):
            callbacks.append(1)
            return api.ToolResultV1(data={'count': 14}, evidence=(api.EvidenceDraftV1(
                'graph', 'r1', 'PRIVATE-LOCATOR', '/count', 'Dependents'),))
        tool = api.CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False}, handler, '1',
            result_contract=api.ToolResultContractV1('1'), event_policy=api.ToolEventPolicyV1('1', {
                'count': api.ToolDisplayFieldV1('result', '/count')}))
        endpoint = api.EndpointConfig(f'http://127.0.0.1:{server.server_port}/v1', 'fixture', provider=provider,
            features={'streaming': False})
        runtime = api.ProsaicRuntime(api.RuntimeConfig({'fixture': endpoint}, {}, 'fixture', frozenset({'lookup'})),
            custom_tools={'lookup': tool})
        scope = api.InvocationScope('attempt')
        selected = {'citationId': 'ctx_' + 'a' * 64, 'content': 'Selected captured context'}
        imported = api.HostEvidenceImportV1(scope, api.EvidenceDraftV1('pack', 'r1', 'PRIVATE-LOCATOR', '', 'Selected'),
            evidence_id=selected['citationId'], origin_kind='contextPack', origin_receipt_sha256='b' * 64,
            model_view=selected, content_pointer='/content', reference_pointer='/citationId')
        bindings = api.ToolContractBindingsV1('host', '1', scope, retained=(imported,),
            evidence_authorizer=api.EvidenceAuthorizerV1('sources', '1', lambda use: True),
            display_authorizer=api.ToolDisplayAuthorizerV1('viewer', '1', lambda use: True), citations=api.CitationPolicyV1('required'))
        agent = api.ProsaicArtifact('approved', 'subagent', {'tools': ['lookup']}, 'Answer from approved tools.')
        policy = api.RunPolicy(allowed_tools=frozenset({'lookup'}), max_provider_requests=2, max_tool_calls=1)
        prepared = runtime.prepare_turn(agent, message='Review', input_data={'selected': selected}, policy=policy, tool_contract_bindings=bindings)
        frozen = prepared.export_frozen_input()
        restored = runtime.restore_turn(agent, frozen_input=frozen, policy=policy, limits=api.TurnLimits(), tool_contract_bindings=bindings)
        turn = runtime.turn(restored, tool_contract_bindings=bindings, on_event=events.append)
        assert turn.result.exit_code == 0 and callbacks == [1] and len(requests) == 2
        evidence = turn.evidence.to_dict()
        assert evidence['version'] == 3 and api.TurnEvidence.from_dict(evidence).to_dict() == evidence
        assert api.CitationsV1.from_dict(evidence['toolContractV1']['citations']).verify_answer(turn.result.stdout)
        assert [e['event'] for e in events if e['event'].startswith('tool_')] == ['tool_started', 'tool_completed']
        assert 'PRIVATE-LOCATOR' not in b''.join(requests).decode() and 'PRIVATE-LOCATOR' not in json.dumps(events)
        assert {'tool_events_v1', 'tool_results_v1', 'citations_v1'} <= runtime.capabilities
        assert selected['citationId'] in evidence['toolContractV1']['evidence']['deliveries']
        legacy = api.CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False},
            lambda args: {'count': 14}, '1', event_policy=tool.event_policy)
        legacy_runtime = api.ProsaicRuntime(runtime.config, custom_tools={'lookup': legacy})
        legacy_events = []
        legacy_bindings = api.ToolContractBindingsV1('viewer', '1', api.InvocationScope('legacy'),
            display_authorizer=bindings.display_authorizer)
        direct = legacy_runtime.run_tool('lookup', {}, policy=policy, tool_contract_bindings=legacy_bindings,
            on_event=legacy_events.append)
        assert json.loads(direct.stdout) == {'count': 14} and len(legacy_events) == 2
        assert direct.metadata['tool_contract_v1']['deliveries'] == {}
        return dict(provider=provider, providerRequests=len(requests), toolCallbacks=len(callbacks))
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)
        assert not worker.is_alive()


def main():
    owner = metadata.distribution('b3-prosaic-runtime')
    assert api.__version__ == owner.version
    origin = Path(api.__file__).resolve()
    assert 'site-packages' in origin.parts and origin in {Path(owner.locate_file(f)).resolve() for f in owner.files}
    assert metadata.packages_distributions()['prosaic_runtime'] == ['b3-prosaic-runtime']
    direct = owner.read_text('direct_url.json')
    assert direct is None or not json.loads(direct).get('dir_info', {}).get('editable', False)
    print(json.dumps(dict(version=owner.version, cases=[exercise(provider) for provider in ('openai-compatible', 'anthropic')]), sort_keys=True))


if __name__ == '__main__': main()
