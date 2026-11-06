"""Credential-free installed-wheel witness for rule admission with a tagged agent.

Run with an environment holding only the release wheels, where Core was
installed to satisfy the Runtime wheel's declared ``b3-prosaic`` dependency.
It fails unless that Core provides ``parse_rules``, so release qualification
cannot pass merely because CI installed Core from git. Only a numeric loopback
fixture is contacted; every agent, guard and value is synthetic.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
import json
from pathlib import Path
import re
from threading import Thread

import prosaic
import prosaic_runtime as api

BODY = (
    'Answer with the shelf mark.\n\n'
    '- Quote shelf marks exactly. <!-- enforce: shelf_check(stage=final, on_violation=retry) -->\n'
    '- Be brief. <!-- prose-only: tone is judged by people -->\n'
)
UNBOUND = 'Answer.\n\n- Cite sources. <!-- enforce: source_check(stage=final, on_violation=fail) -->\n'


def installed():
    for module, distribution in ((prosaic, 'b3-prosaic'), (api, 'b3-prosaic-runtime')):
        owner = metadata.distribution(distribution)
        origin = Path(module.__file__).resolve()
        assert 'site-packages' in origin.parts, (distribution, origin)
        assert origin in {Path(owner.locate_file(f)).resolve() for f in owner.files}, (distribution, origin)
    names = {
        re.match(r'[A-Za-z0-9._-]+', r).group(0).lower().replace('_', '-')
        for r in metadata.requires('b3-prosaic-runtime') or ()
    }
    assert 'b3-prosaic' in names, 'the Runtime wheel must declare its Core dependency'
    if not callable(getattr(prosaic, 'parse_rules', None)) or not callable(getattr(prosaic, 'strip_tags', None)):
        raise SystemExit(
            f'installed b3-prosaic {metadata.version("b3-prosaic")} lacks parse_rules/strip_tags: raise the '
            'b3-prosaic dependency floor (and the release pins) to the Core release that provides them'
        )
    assert 'rule_admission_v1' in api.ProsaicRuntime.capabilities


def main():
    installed()
    requests, answers = [], ['Shelf unknown.', 'Shelf QA-104.']

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body = json.dumps(
                {
                    'choices': [{'message': {'role': 'assistant', 'content': answers.pop(0)}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = api.RuntimeConfig(
            {
                'fixture': api.EndpointConfig(
                    f'http://127.0.0.1:{server.server_port}/v1', 'synthetic', features={'streaming': False}
                )
            },
            {},
            'fixture',
            frozenset(),
        )

        def shelf_check(stage, context):
            ok = 'QA-' in context.draft
            return api.Verdict(ok, refused_values=() if ok else (context.draft,))

        guard = api.Guard('shelf_check', 'shelf-check-v1', ('final',), shelf_check)
        runtime = api.ProsaicRuntime(config, guards=[guard])
        policy = api.RunPolicy(timeout_s=5, max_provider_requests=2, max_tool_calls=0)
        agent = api.ProsaicArtifact('synthetic/shelf', 'subagent', {}, BODY)
        assert runtime.rule_coverage(agent) == {
            'registered': ['synthetic/shelf:3:shelf_check'],
            'unregistered': [],
            'proseOnly': ['synthetic/shelf:4:prose-only'],
            'issues': 0,
        }
        turn = runtime.turn(agent, message='Which shelf?', policy=policy)
        assert turn.result.exit_code == 0 and turn.result.stdout == 'Shelf QA-104.'
        assert turn.rules_violated == ('synthetic/shelf:3:shelf_check',) and turn.rules_faulted == ()
        assert turn.rules_fingerprint and len(requests) == 2
        system = requests[0]['messages'][0]['content']
        assert '<!--' not in system and 'Quote shelf marks exactly.' in system  # Core strip_tags text only.
        assert 'Quote shelf marks exactly.' in json.dumps(requests[1]['messages'][1:])
        try:
            runtime.turn(api.ProsaicArtifact('synthetic/unbound', 'subagent', {}, UNBOUND), message='Go', policy=policy)
        except api.TurnAdmissionError as exc:
            assert exc.code == 'invalidArtifact'
        else:
            raise AssertionError('an unregistered fail guard was admitted')
        assert len(requests) == 2
        print(
            json.dumps(
                {
                    'version': 1,
                    'runtimeVersion': api.__version__,
                    'coreVersion': metadata.version('b3-prosaic'),
                    'checks': [
                        'installedOwnership',
                        'coreParseRules',
                        'coverage',
                        'retryEnforced',
                        'tagsStripped',
                        'unboundRefused',
                    ],
                    'providerRequests': len(requests),
                    'liveProvider': False,
                }
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        assert not thread.is_alive()


if __name__ == '__main__':
    main()
