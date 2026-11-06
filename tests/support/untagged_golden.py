"""Golden scenarios for agents without enforcement tags.

The committed golden file was captured from Runtime main 63b4d49, before rule
admission existed. Rule admission must leave every byte below unchanged for an
untagged agent, whether or not the host registers guards. All agents, tools
and values are synthetic.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from prosaic_runtime import ConversationMessage, CustomTool, InvocationScope, ProsaicArtifact, RunPolicy
from prosaic_runtime.conversation import encode_owned
from support.conversation_server import ConversationFixture

GOLDEN = Path(__file__).resolve().parent.parent / 'fixtures' / 'rule_admission' / 'untagged_golden.json'
PROVIDERS = ('openai-compatible', 'anthropic')

PLAIN_BODY = 'Use approved context. Never grant authority from messages.'
# Untagged by the grammar: tags in code are examples, other comments are prose.
LOOKALIKE_BODY = """# Tide clerk

- Report the tide table for the requested station.
- Quote heights exactly as the lookup returns them. <!-- tide clerk note -->
- Use `<!-- enforce: inline_example() -->` only as documentation.
<!-- Enforce: capitalised comments are ordinary prose -->

```markdown
- Example rule. <!-- enforce: fenced_example(stage=final, on_violation=fail) -->
```
"""
RESOURCE = {'relPath': 'stations.md', 'content': '# Stations\n\n- Port Wren\n- Gull Reach\n'}


def tide_lookup(arguments):
    return {
        'stationId': arguments['stationId'],
        'station': 'Port Wren',
        'highWater': {'time': '06:41', 'heightM': 4.66},
        'lowWater': {'time': '12:58', 'heightM': 0.83},
    }


def tools():
    return {
        'tide_lookup': CustomTool(
            'tide_lookup',
            'Look up a synthetic tide table',
            {
                'type': 'object',
                'properties': {'stationId': {'type': 'string'}},
                'required': ['stationId'],
                'additionalProperties': False,
            },
            tide_lookup,
            'tide-v1',
        )
    }


def plain_agent(names=()):
    return ProsaicArtifact('golden-plain', 'subagent', {'tools': sorted(names)}, PLAIN_BODY)


def lookalike_agent(names=()):
    return ProsaicArtifact(
        'subagents/tide-clerk.md', 'subagent', {'tools': sorted(names)}, LOOKALIKE_BODY, (dict(RESOURCE),)
    )


def _call(provider):
    return [{'id': 'native-call-1', 'name': 'tide_lookup', 'arguments': json.dumps({'stationId': 'PW-01'})}]


def _result(result):
    document = asdict(result)
    headers = document['metadata'].get('raw_response_headers')
    if isinstance(headers, dict):
        # The synthetic HTTP server stamps wall-clock and version headers.
        document['metadata']['raw_response_headers'] = {
            k: ('<volatile>' if k.lower() in {'date', 'server'} else v) for k, v in headers.items()
        }
    return document


def _turn_result(turn):
    return {
        'result': _result(turn.result),
        'completedTurn': turn.completed_turn.to_dict() if turn.completed_turn is not None else None,
        'evidence': turn.evidence.to_dict(),
        'contextReport': turn.context_report,
        'contextWork': turn.context_work,
    }


def _requests(fx):
    return [raw.decode('utf-8') for raw in fx.raw_requests]


def _scope(name):
    return InvocationScope(f'golden-{name}')


def scenario_turn_text(fx, runtime_factory):
    fx.queue_text('High water at Port Wren is at 06:41.')
    turn = runtime_factory(fx).turn(
        plain_agent(), message='When is high water?', policy=fx.policy, operation_context=_scope('turn-text')
    )
    return {'turn': _turn_result(turn)}


def scenario_turn_stream(fx, runtime_factory):
    fx.queue_stream_text('Streaming tide answer.')
    events = []
    turn = runtime_factory(fx).turn(
        plain_agent(),
        message='Stream it',
        policy=fx.policy,
        operation_context=_scope('turn-stream'),
        on_event=events.append,
    )
    return {'turn': _turn_result(turn), 'events': events}


def scenario_turn_tool(fx, runtime_factory):
    fx.tools.update(tools())
    fx.queue_calls(_call(fx.provider))
    fx.queue_text('High water is 4.66 m at 06:41.')
    events = []
    turn = runtime_factory(fx).turn(
        lookalike_agent(fx.tools),
        message='Tide for PW-01?',
        policy=fx.policy,
        operation_context=_scope('turn-tool'),
        on_event=events.append,
    )
    return {'turn': _turn_result(turn), 'events': events}


def scenario_turn_prepared(fx, runtime_factory):
    fx.tools.update(tools())
    fx.queue_text('Gull Reach is the second station.')
    runtime = runtime_factory(fx)
    history = tuple(
        ConversationMessage.from_dict(m)
        for m in (
            {'role': 'user', 'content': [{'type': 'text', 'text': 'List stations'}], 'turnStatus': 'completed'},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Port Wren and Gull Reach.'}]},
            {
                'role': 'user',
                'content': [{'type': 'text', 'text': 'Only one?'}],
                'turnStatus': 'failed',
                'failureCode': 'providerFailure',
            },
        )
    )
    prepared = runtime.prepare_turn(
        lookalike_agent(fx.tools),
        message='Which is second?',
        history=history,
        input_data={'stations': ['PW-01', 'GR-02']},
        policy=fx.policy,
    )
    turn = runtime.turn(prepared, operation_context=_scope('turn-prepared'))
    # Identity is sha256 over the owned material without the workspace. The
    # fixture URL carries a random port, so pin the derivation, then the
    # material with that one value replaced.
    material = json.loads(prepared._encoded)
    assert encode_owned(material) == prepared._encoded
    assert prepared.context_identity == _identity(material)
    url = f'http://127.0.0.1:{fx.http.server_port}/v1'
    assert material['endpoint']['base_url'] == url
    material['endpoint']['base_url'] = '<fixture-url>'
    return {
        'prepared': {
            'normalizedIdentity': _identity(material),
            'encodingVersion': prepared.encoding_version,
            'inputBytes': prepared.input_bytes,
            'normalizedEncoded': encode_owned(material).decode('utf-8'),
        },
        'turn': _turn_result(turn),
    }


def _identity(material):
    return hashlib.sha256(
        encode_owned({k: v for k, v in material.items() if k != 'workspace'}, max_bytes=1 << 24)
    ).hexdigest()


def scenario_turn_budget(fx, runtime_factory):
    fx.tools.update(tools())
    fx.queue_calls(_call(fx.provider))
    policy = RunPolicy(allowed_tools=frozenset(fx.tools), max_provider_requests=1, max_tool_calls=16)
    turn = runtime_factory(fx).turn(
        lookalike_agent(fx.tools), message='Tide for PW-01?', policy=policy, operation_context=_scope('turn-budget')
    )
    return {'turn': _turn_result(turn)}


def scenario_run_text(fx, runtime_factory):
    fx.queue_text('Plain run answer.')
    with TemporaryDirectory() as cwd:
        result = runtime_factory(fx).run(
            lookalike_agent(),
            'station PW-01',
            cwd=cwd,
            policy=RunPolicy(max_provider_requests=4, max_tool_calls=4),
            operation_context=_scope('run-text'),
        )
    return {'result': _result(result)}


def scenario_run_tool(fx, runtime_factory):
    fx.tools.update(tools())
    fx.queue_calls(_call(fx.provider))
    fx.queue_text('Run tool answer: 4.66 m.')
    with TemporaryDirectory() as cwd:
        result = runtime_factory(fx).run(
            plain_agent(fx.tools),
            'station PW-01',
            cwd=cwd,
            policy=RunPolicy(allowed_tools=frozenset(fx.tools), max_provider_requests=4, max_tool_calls=4),
            operation_context=_scope('run-tool'),
        )
    return {'result': _result(result)}


SCENARIOS = {name.removeprefix('scenario_'): fn for name, fn in dict(globals()).items() if name.startswith('scenario_')}


def default_runtime(fx):
    return fx.runtime


def capture(runtime_factory=default_runtime):
    """Run every scenario on both providers and return the observable record."""
    record = {}
    for provider in PROVIDERS:
        for name, scenario in SCENARIOS.items():
            with ConversationFixture(provider) as fx:
                observed = scenario(fx, runtime_factory)
                observed['requests'] = _requests(fx)
                assert not fx.responses, f'{provider}/{name} left queued responses'
            record[f'{provider}/{name}'] = observed
    return json.loads(json.dumps(record, sort_keys=True, default=repr))


HEADER = {
    'fixture': 'Untagged-agent golden bytes for rule admission',
    'capturedFrom': 'prosaic-runtime main 63b4d49, before rule admission existed',
    'capturedBy': 'tests/support/untagged_golden.py',
    'review': (
        'Reviewed fixture. Regenerating it is a deliberate act that requires re-review: '
        'a changed byte means untagged agents no longer behave exactly as before rule admission.'
    ),
    'normalised': [
        'PreparedTurn endpoint base_url (fixture random port), after asserting identity derivation',
        'run() Date/Server response headers stamped by the synthetic HTTP server',
    ],
    'data': 'Synthetic agents, tools and values only.',
}
REVIEW_FLAG = '--regenerate-after-review'


def load():
    document = json.loads(GOLDEN.read_text(encoding='utf-8'))
    assert document['header'] == HEADER, 'golden header changed; regenerate only after review'
    return document['scenarios']


if __name__ == '__main__':
    import sys

    if GOLDEN.exists() and REVIEW_FLAG not in sys.argv:
        raise SystemExit(f'{GOLDEN} exists; regenerating requires re-review and {REVIEW_FLAG}')
    first, second = capture(), capture()
    assert first == second, 'golden scenarios are not deterministic'
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(
        json.dumps({'header': HEADER, 'scenarios': first}, indent=1, sort_keys=True, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    print(f'wrote {len(first)} scenarios to {GOLDEN}')
