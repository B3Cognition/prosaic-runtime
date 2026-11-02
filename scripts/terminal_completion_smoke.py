"""Credential-free installed-wheel witness for host-owned native completion."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import prosaic_runtime as api


def main():
    assert 'site-packages' in Path(api.__file__).resolve().parts
    assert api.__version__ == metadata.version('b3-prosaic-runtime')
    requests, effects, events, observations = [], [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            call = {'id': 'complete-1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}
            body = json.dumps({'choices': [{'message': {'content': '', 'tool_calls': [call]},
                'finish_reason': 'tool_calls'}], 'usage': {
                'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        def lookup(arguments):
            effects.append(1)
            return api.TerminalCompletion({'complete': True}, artifact={'entityId': 'sample-1'})
        tool = api.CustomTool('lookup', 'Look up a synthetic entity',
            {'type': 'object', 'additionalProperties': False}, lookup, 'terminal-v1', terminal=True)
        config = api.RuntimeConfig({'local': api.EndpointConfig(
            f'http://127.0.0.1:{server.server_port}/v1', 'fixture', features={'streaming': False})},
            {}, 'local', frozenset({'lookup'}))
        runtime = api.ProsaicRuntime(config, custom_tools={'lookup': tool})
        agent = api.ProsaicArtifact('approved', 'subagent', {'name': 'finder',
            'description': 'Find a synthetic entity', 'tools': ['lookup']},
            'ALWAYS use the approved lookup. NEVER invent an entity.')
        policy = api.RunPolicy(allowed_tools=frozenset({'lookup'}),
            allowed_terminal_tools=frozenset({'lookup'}), max_provider_requests=2, max_tool_calls=1)
        with TemporaryDirectory(prefix='terminal-wheel-') as directory:
            result = runtime.run(agent, cwd=directory, policy=policy,
                operation_context=api.InvocationScope('installed-attempt'),
                on_event=events.append, observer=observations.append)
        assert result.exit_code == 0 and json.loads(result.stdout) == {'complete': True}
        assert len(requests) == 1 and effects == [1] and result.token_usage == 5
        assert result.metadata['terminalCompletion']['artifact'] == {'entityId': 'sample-1'}
        assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1
        assert 'sample-1' not in json.dumps(events) + json.dumps(observations)
        print(json.dumps({'terminalCompletion': 'passed', 'providerRequests': 1, 'toolCalls': 1,
                          'runtimeVersion': api.__version__}))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        assert not thread.is_alive()


if __name__ == '__main__':
    main()
