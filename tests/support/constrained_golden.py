"""Digests of a default (opt-in absent) correcting turn, pinned against the pre-feature main branch.

The pins in test_constrained_correction_wire.py were captured at main 5176bdd before any source change.
Run ``PYTHONPATH=tests python -m support.constrained_golden`` to print them.
"""

from dataclasses import replace
import hashlib
import json
import re

from prosaic_runtime import EndpointConfig, ProsaicRuntime
from support.conversation_server import ConversationFixture
from support.constrained_fixtures import Tools, VALID, calls, scripted


_RANDOM_ID = re.compile(r'\b(?:[0-9a-f]{64}|[0-9a-f]{32})\b')


def digest(value, ids):
    """SHA-256 after replacing per-invocation random identifiers by their order of first appearance."""
    text = value.decode() if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(',', ':'))
    text = _RANDOM_ID.sub(lambda m: f'<id{ids.setdefault(m.group(), len(ids))}>', text)
    return hashlib.sha256(text.encode()).hexdigest()


def observe():
    """Run an ordinary correction to an answer and an unprepared-turn identity; return their digests."""
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        config = fx.runtime.config
        endpoint = config.profiles['local']
        correcting = ProsaicRuntime(
            replace(config, profiles={'local': replace(endpoint, features={**endpoint.features, 'streaming': False})}),
            custom_tools=fx.tools,
            correct_malformed_tool_calls=True,
        )
        fx.responses.clear()
        scripted(fx, ('find_station', '{"query":"North Pier"}'), None)
        # The ordinary correction: the weak model resends a valid call itself.
        fx.responses[1] = calls(('again', 'find_station', json.dumps(VALID['find_station'])))
        policy = replace(fx.policy, max_provider_requests=3, max_tool_calls=1)
        turn = correcting.turn(fx.agent, message='Which station is North Pier?', policy=policy)
        ids = {}
        requests = [digest(raw, ids) for raw in fx.raw_requests]
        fixed = replace(
            correcting.config,
            profiles={
                'local': EndpointConfig(
                    'http://127.0.0.1:9/v1',
                    'fixture-model',
                    features=dict(correcting.config.profiles['local'].features),
                )
            },
        )
        prepared = ProsaicRuntime(fixed, custom_tools=fx.tools, correct_malformed_tool_calls=True).prepare_turn(
            fx.agent, message='Which station is North Pier?', policy=policy
        )
    metadata = {k: v for k, v in turn.result.metadata.items() if k != 'provider_transcript_path'}
    return {
        'requests': requests,
        'prepared': digest(prepared._encoded, {}),
        'identity': prepared.context_identity,
        'evidence': digest(turn.evidence.to_dict(), ids),
        'completed': digest(turn.completed_turn.to_dict(), ids),
        'metadata': digest(metadata, ids),
        'exit': turn.result.exit_code,
        'corrections': list(turn.tool_request_corrections),
    }


if __name__ == '__main__':
    print(json.dumps(observe(), indent=1))
