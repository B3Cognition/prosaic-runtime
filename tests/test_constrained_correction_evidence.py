"""Wire shape around a constrained correction, and its evidence and identity on every exit path."""

from dataclasses import fields, replace
import hashlib
import json

import pytest

from prosaic_runtime import CustomTool, RuntimeConfig
from prosaic_runtime.conversation import TurnResult
from support.constrained_fixtures import FEATURE, VALID, WIRES, Tools, calls, constraint, runtime, scripted, text
from support.constrained_golden import _RANDOM_ID
from support.conversation_server import ConversationFixture
from test_tool_request_correction import strictly_alternating

MESSAGE = 'Report the synthetic gauge reading.'
KEY = 'tool_request_correction_constraint'
BAD_FIND = ('find_station', '{"query":"North Pier"}')


def resend(name, arguments=None):
    return {'tool': name, 'arguments': VALID[name] if arguments is None else arguments}


def policy(fx, requests=3):
    return replace(fx.policy, max_provider_requests=requests, max_tool_calls=1)


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def normalized(value):
    ids = {}
    return _RANDOM_ID.sub(lambda m: f'<id{ids.setdefault(m.group(), len(ids))}>', json.dumps(value, sort_keys=True))


def ordinary_resend(fx, malformed, name):
    """The ordinary arm: the model itself resends exactly the call the constrained decoder would produce."""
    fx.responses.append(calls(('first', *malformed)))
    fx.responses.append(calls(('again', name, json.dumps(VALID[name], ensure_ascii=False))))
    fx.responses.append(text('Answer.'))


# Wire shape.


@pytest.mark.parametrize('wire', WIRES)
def test_the_messages_around_the_correction_match_ordinary_correction_byte_for_byte(wire):
    with ConversationFixture('openai-compatible') as constrained:
        Tools(constrained)
        scripted(constrained, BAD_FIND, resend('find_station'), answer='Answer.')
        runtime(constrained, wire).turn(constrained.agent, message=MESSAGE, policy=policy(constrained))
    with ConversationFixture('openai-compatible') as ordinary:
        Tools(ordinary)
        ordinary_resend(ordinary, BAD_FIND, 'find_station')
        runtime(ordinary).turn(ordinary.agent, message=MESSAGE, policy=policy(ordinary))
    for index in (1, 2):  # The correction request and the answer request.
        mine, theirs = constrained.requests[index]['messages'], ordinary.requests[index]['messages']
        assert normalized(mine) == normalized(theirs)
        roles = [m['role'] for m in mine if m['role'] != 'system']
        assert strictly_alternating(roles)
    placeholder, envelope = constrained.requests[1]['messages'][-2:]
    assert placeholder['role'] == 'assistant' and envelope['role'] == 'user'


@pytest.mark.parametrize('wire', WIRES)
def test_only_the_correction_request_differs_from_ordinary_correction(wire):
    with ConversationFixture('openai-compatible') as constrained:
        Tools(constrained)
        scripted(constrained, BAD_FIND, resend('find_station'), answer='Answer.')
        runtime(constrained, wire).turn(constrained.agent, message=MESSAGE, policy=policy(constrained))
    with ConversationFixture('openai-compatible') as ordinary:
        Tools(ordinary)
        ordinary_resend(ordinary, BAD_FIND, 'find_station')
        runtime(ordinary).turn(ordinary.agent, message=MESSAGE, policy=policy(ordinary))
    first, correction, answer = constrained.requests
    assert normalized(first) == normalized(ordinary.requests[0])
    assert normalized(answer) == normalized(ordinary.requests[2])
    differing = {
        key
        for key in set(correction) | set(ordinary.requests[1])
        if correction.get(key) != ordinary.requests[1].get(key)
    }
    assert differing == (
        {'tool_choice', 'response_format'} if wire == 'json_schema' else {'tool_choice', 'structured_outputs'}
    )


@pytest.mark.parametrize('finish_reason', ['length', 'tool_calls', 'content_filter', None])
@pytest.mark.parametrize('wire', WIRES)
def test_valid_content_with_any_finish_reason_but_stop_is_refused(wire, finish_reason):
    from support.constrained_fixtures import completion

    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.extend(
            [
                calls(('first', *BAD_FIND)),
                completion({'content': json.dumps(resend('find_station'))}, finish_reason),
                text('x'),
            ]
        )
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert turn.result.exit_code == 1 and tools.reads == [] and failure(turn) == 'providerFailure'


@pytest.mark.parametrize('wire', WIRES)
def test_unicode_arguments_survive_to_the_read_and_the_wire(wire):
    arguments = {'query': 'Nördliche Mole – 北の桟橋', 'limit': 1}
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, BAD_FIND, resend('find_station', arguments), answer='Answer.')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        sent = fx.requests[2]['messages'][-2]['tool_calls'][0]['function']['arguments']
    assert turn.result.exit_code == 0 and tools.reads == [('find_station', arguments)]
    assert json.loads(sent) == arguments and 'Nördliche' in sent  # ensure_ascii=False in the call arguments.


DEEP = '{"tool":"find_station","arguments":{"query":"a","limit":1,"x":' + '[' * 70 + ']' * 70 + '}}'


@pytest.mark.parametrize(
    'content',
    [
        '{"tool":"find_station","tool":"find_station","arguments":{"query":"a","limit":1}}',
        '{"tool":"find_station","arguments":{"query":"a","limit":NaN}}',
        '{"tool":"find_station","arguments":{"query":"a","limit":Infinity}}',
        '{"tool":"find_station","arguments":{"query":"a","limit":-Infinity}}',
        DEEP,
    ],
    ids=['duplicate-key', 'nan', 'infinity', 'negative-infinity', 'over-deep'],
)
@pytest.mark.parametrize('wire', WIRES)
def test_unsafe_json_is_refused_before_any_read(wire, content):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.extend([calls(('first', *BAD_FIND)), text(content), text('x')])
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert failure(turn) == 'providerFailure' and tools.reads == [] and tools.authorized == []


@pytest.mark.parametrize('wire', WIRES)
def test_an_empty_object_is_accepted_for_a_tool_without_required_fields(wire):
    schema = {'type': 'object', 'properties': {'verbose': {'type': 'boolean'}}, 'additionalProperties': False}
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx, names=('ping',), schemas={'ping': schema})
        fx.responses.extend(
            [calls(('first', 'ping', '{"verbose":"yes"}')), text('{"tool":"ping","arguments":{}}'), text('Pong.')]
        )
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert turn.result.exit_code == 0 and tools.reads == [('ping', {})]


# The recorded digest is of the exact schema bytes on the wire.


def wire_schema_bytes(raw, wire):
    """Slice the schema value out of the raw request body, byte for byte."""
    marker = b'"schema": ' if wire == 'json_schema' else b'"structured_outputs": {"json": '
    start = raw.index(marker) + len(marker)
    _, end = json.JSONDecoder().raw_decode(raw.decode('utf-8'), start)
    return raw[start:end]


@pytest.mark.parametrize('case', ['argumentsSchema', 'toolNotGranted'])
@pytest.mark.parametrize('wire', WIRES)
def test_the_schema_digest_is_the_sha256_of_the_bytes_sent(wire, case):
    malformed = BAD_FIND if case == 'argumentsSchema' else ('find_stations', json.dumps(VALID['find_station']))
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, malformed, resend('find_station'), answer='Answer.')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        sent = wire_schema_bytes(fx.raw_requests[1], wire)
    assert hashlib.sha256(sent).hexdigest() == turn.result.metadata[KEY]['schemaSha256']


# Evidence on every exit path.


def record(turn):
    return turn.result.metadata.get(KEY)


def applied(wire, issue='argumentsSchema'):
    def check(value):
        return (
            value is not None
            and value['wire'] == wire
            and value['issue'] == issue
            and len(value['schemaSha256']) == 64
            and 'fallback' not in value
        )

    return check


@pytest.mark.parametrize('wire', WIRES)
def test_evidence_on_success(wire):
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, BAD_FIND, resend('find_station'), answer='Answer.')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert turn.result.exit_code == 0 and applied(wire)(record(turn))


@pytest.mark.parametrize('wire', WIRES)
def test_evidence_on_an_unhonoured_constraint(wire):
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        fx.responses.extend([calls(('first', *BAD_FIND)), text('Prose.'), text('x')])
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert failure(turn) == 'providerFailure' and applied(wire)(record(turn))


@pytest.mark.parametrize('wire', WIRES)
def test_evidence_on_invalid_tool_request_after_the_correction(wire):
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, BAD_FIND, resend('find_station', {'query': 'North Pier', 'limit': 9}))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert failure(turn) == 'invalidToolRequest' and applied(wire)(record(turn))


@pytest.mark.parametrize('wire', WIRES)
def test_evidence_on_an_authorisation_refusal(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx, authorize=False)
        scripted(fx, BAD_FIND, resend('find_station'), answer='Not permitted.')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert tools.reads == [] and applied(wire)(record(turn))


@pytest.mark.parametrize('wire', WIRES)
def test_evidence_when_the_answer_exceeds_the_request_budget(wire):
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, BAD_FIND, resend('find_station'), answer='unused')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx, 2))
    assert failure(turn) == 'budgetExceeded' and applied(wire)(record(turn))


@pytest.mark.parametrize('wire', WIRES)
def test_evidence_on_a_deadline(wire):
    import time

    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        fx.responses.append(calls(('first', *BAD_FIND)))
        fx.responses.append(lambda request: time.sleep(0.6) or text(json.dumps(resend('find_station'))))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=replace(policy(fx), timeout_s=0.3))
    assert failure(turn) == 'timeout' and applied(wire)(record(turn))


@pytest.mark.parametrize(
    'reason',
    ['issue', 'batch', 'oversize'],
)
@pytest.mark.parametrize('wire', WIRES)
def test_evidence_on_every_reachable_fallback(wire, reason):
    with ConversationFixture('openai-compatible') as fx:
        if reason == 'oversize':
            big = {
                'type': 'object',
                'additionalProperties': False,
                'required': ['code'],
                'properties': {
                    'code': {'type': 'string', 'enum': [f'code-{index:05d}-padding-padding' for index in range(700)]}
                },
            }
            Tools(fx, names=('find_station',), schemas={'find_station': big})
            first = calls(('first', 'find_station', '{"code":"none"}'))
        else:
            Tools(fx)
            first = (
                calls(('first', 'find_station', '{"query": oops}'))
                if reason == 'issue'
                else calls(('a', 'find_station', json.dumps(VALID['find_station'])), ('b', *BAD_FIND))
            )
        fx.responses.extend([first, text('Answer without the tool.')])
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert all(constraint(request) is None for request in fx.requests)
    issue = 'argumentsMalformed' if reason == 'issue' else 'argumentsSchema'
    assert record(turn) == {'version': 1, 'wire': wire, 'issue': issue, 'fallback': reason}


@pytest.mark.parametrize('wire', WIRES)
def test_no_evidence_when_no_correction_ran(wire):
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        fx.responses.extend([calls(('ok', 'find_station', json.dumps(VALID['find_station']))), text('ST-104.')])
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert turn.result.exit_code == 0 and KEY not in turn.result.metadata


# Identity and schemas: nothing outside the endpoint profile moves.

RUNTIME_CONFIG_FIELDS = [
    'profiles',
    'routes',
    'default_profile',
    'allowed_tools',
    'limits',
    'tool_directories',
    'cli_sandbox',
    'required_guards',
]
TURN_RESULT_FIELDS = [
    'result',
    'completed_turn',
    'evidence',
    'context_report',
    'context_work',
    'rules_evaluated',
    'rules_violated',
    'rules_fixed',
    'rules_faulted',
    'rules_fingerprint',
    'sub_agent_proof',
    'rules_declined',
    'rules_retried',
    'rule_retries_used',
    'tool_request_corrections',
]


def test_runtime_config_and_turn_result_fields_are_unchanged():
    assert [f.name for f in fields(RuntimeConfig)] == RUNTIME_CONFIG_FIELDS
    assert [f.name for f in fields(TurnResult)] == TURN_RESULT_FIELDS


def keys(value, prefix=''):
    if isinstance(value, dict):
        return {f'{prefix}{k}' for k in value} | {p for k, v in value.items() for p in keys(v, f'{prefix}{k}.')}
    return set()


@pytest.mark.parametrize('wire', WIRES)
def test_evidence_and_completed_turn_schemas_match_ordinary_correction(wire):
    turns = {}
    for arm in (None, wire):
        with ConversationFixture('openai-compatible') as fx:
            Tools(fx)
            scripted(fx, BAD_FIND, resend('find_station'), answer='Answer.')
            if arm is None:
                fx.responses[1] = calls(('again', 'find_station', json.dumps(VALID['find_station'])))
            turns[arm] = runtime(fx, arm).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert keys(turns[wire].evidence.to_dict()) == keys(turns[None].evidence.to_dict())
    assert normalized(turns[wire].completed_turn.to_dict()) == normalized(turns[None].completed_turn.to_dict())
    assert set(turns[wire].result.metadata) - set(turns[None].result.metadata) == {KEY}


def without_feature(value):
    if isinstance(value, dict):
        return {k: without_feature(v) for k, v in value.items() if k != FEATURE}
    if isinstance(value, list):
        return [without_feature(v) for v in value]
    return value


@pytest.mark.parametrize('wire', WIRES)
def test_the_prepared_turn_differs_only_in_the_endpoint_feature(wire):
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        off = json.loads(runtime(fx).prepare_turn(fx.agent, message=MESSAGE, policy=policy(fx))._encoded)
        on = json.loads(runtime(fx, wire).prepare_turn(fx.agent, message=MESSAGE, policy=policy(fx))._encoded)
    added = keys(on) - keys(off)
    assert added and all(k.endswith(f'features.{FEATURE}') for k in added)
    assert without_feature(on) == off


def test_the_feature_is_refused_on_tool_shapes_outside_custom_tools():
    # CustomTool refuses the malformed shapes plan() defends against: they reach plan() only from a host registry.
    for schema in (
        {'type': 'object', 'properties': [1]},
        {'type': 'object', 'additionalProperties': False, 'properties': {'a': {'items': [{'type': 'string'}]}}},
    ):
        with pytest.raises(ValueError):
            CustomTool('probe', 'd', schema, lambda _: None, 'v1')


def test_the_documented_keywords_bounds_and_fallback_reasons_match_the_code():
    from pathlib import Path

    from prosaic_runtime import constrained_correction as cc

    docs = Path(__file__).resolve().parents[1] / 'docs' / 'conversations.md'
    section = docs.read_text().split('### Constrained tool request correction', 1)[1].split('\n## ', 1)[0]
    assert all(f'`{keyword}`' in section for keyword in cc.KEYWORDS)
    assert cc.MAX_SCHEMA_BYTES == 16 * 1024 and '16 KiB' in section
    assert cc.MAX_SCHEMA_DEPTH == 64 and 'depth limit of 64' in section
    assert cc.MAX_SCHEMA_NODES == 65_536 and '65,536 node limit' in section
    assert cc.MAX_DROPPED_NAMES == 32 and cc.MAX_DROPPED_BYTES == 1024 and '32 names in\n1,024 bytes' in section
    assert all(f'`{reason}`' in section for reason in ('oversize', 'unsupported', 'issue', 'batch'))
    assert cc.WIRES == ('json_schema', 'omlx_grammar') and all(f'`{wire}`' in section for wire in cc.WIRES)
