"""Guards see what the model saw, from custom and built-in tools, and unseen values never pass."""

from dataclasses import replace
import json

import pytest

from prosaic_runtime import CustomTool, ProsaicRuntime, generic_guards
from prosaic_runtime.rule_facts import tool_facts
from support.conversation_server import ConversationFixture
from support.rules_support import tagged

PROVIDERS = ['openai-compatible', 'anthropic']
VALUES = (
    '- State only figures the files returned. <!-- enforce: value_in_tool_output(stage=final, on_violation=fail) -->\n'
)
IDS = (
    '- Look up only records already seen. '
    '<!-- enforce: args_reference_seen_ids(keys=["recordId"], pattern="REC-*", stage=before_tool, on_violation=fail) -->\n'
)


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def add_lookup(fx, calls):
    def lookup(arguments):
        calls.append(arguments)
        return {'shelf': 'North'}

    fx.tools['record_lookup'] = CustomTool(
        'record_lookup',
        'Look up a synthetic record',
        {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'recordId': {'type': 'string'},
                'reference': {'type': 'string'},
                'batches': {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'string'}}},
            },
        },
        lookup,
        'record-v1',
    )


def workspace_turn(fx, tmp_path, body, *, message='Check the ledger'):
    """A turn granted read_file over tmp_path plus the fixture's custom tools."""
    tools = frozenset({'read_file', *fx.tools})
    config = replace(fx.runtime.config, allowed_tools=fx.runtime.config.allowed_tools | {'read_file'})
    runtime = ProsaicRuntime(config, custom_tools=fx.tools, guards=generic_guards())
    policy = replace(fx.policy, allowed_tools=tools, read_roots=(str(tmp_path),))
    return runtime.turn(tagged(body, tools=tools), message=message, policy=policy, workspace=tmp_path)


def read(fx, name):
    fx.queue_calls([{'id': 'read-1', 'name': 'read_file', 'arguments': json.dumps({'path': name})}])


# Fact projection -------------------------------------------------------------------


def test_built_in_results_become_facts_without_envelope_bookkeeping():
    payload = {
        'status': 'ok',
        'path': 'ledger.txt',
        'offset': 40,
        'sha256': 'a' * 64,
        'lines_read': 2,
        'line_count': 900,
        'truncated': True,
        'content': '41: Total EUR 125\n42: REC-123',
    }
    assert tool_facts('read_file', payload) == {'path': 'ledger.txt', 'lines': ['Total EUR 125', 'REC-123']}
    many = {
        'status': 'ok',
        'truncated': True,
        'files': [
            {'path': 'a.txt', 'status': 'ok', 'line_count': 3, 'truncated': True, 'content': '1: x\n2: y\n3'},
            {'path': 'b.txt', 'status': 'skipped', 'error': 'outside dispatch read scope'},
        ],
    }
    assert tool_facts('read_many_files', many) == {'files': [{'path': 'a.txt', 'lines': ['x', 'y']}]}
    grep = {
        'status': 'ok',
        'pattern': 'EUR',
        'matches': [{'path': 'a', 'line': 7, 'text': 'EUR 9', 'context': '7: EUR 9'}],
    }
    assert tool_facts('grep_files', grep) == {'matches': [{'path': 'a', 'text': 'EUR 9'}]}
    assert tool_facts('grep_context', grep) == {'matches': [{'path': 'a', 'lines': ['EUR 9']}]}
    tree = {'status': 'ok', 'path': '.', 'entries': [{'path': 'a', 'type': 'file', 'size': 4242}], 'truncated': False}
    assert tool_facts('list_tree_with_sizes', tree) == {'paths': ['a']}
    assert tool_facts('sha256_file', {'status': 'ok', 'path': 'a', 'sha256': 'b' * 64, 'bytes': 12}) == {
        'path': 'a',
        'sha256': 'b' * 64,
    }
    assert tool_facts('list_files', {'status': 'ok', 'pattern': '*', 'matches': ['a'], 'truncated': False}) == {
        'paths': ['a']
    }
    for name in ('write_file', 'edit_file'):  # The path is the model's own input, not evidence.
        assert tool_facts(name, {'status': 'ok', 'path': 'notes/REC-9999.md', 'bytes': 9}) is None


def test_a_line_cut_by_a_character_budget_is_never_a_fact():
    cut = {'path': 'a.txt', 'status': 'ok', 'line_count': 9, 'truncated': True, 'content': '1: REC-1234\n2: 1500'}
    whole = {**cut, 'truncated': False}
    assert tool_facts('read_many_files', {'status': 'ok', 'files': [cut]}) == {
        'files': [{'path': 'a.txt', 'lines': ['REC-1234']}]
    }
    assert tool_facts('read_many_files', {'status': 'ok', 'files': [whole]})['files'][0]['lines'] == [
        'REC-1234',
        '1500',
    ]


def test_custom_results_errors_refusals_and_unknown_shapes():
    assert tool_facts('record_lookup', {'status': 'ok', 'result': {'amount': 3}}) == {'amount': 3}
    assert tool_facts('read_file', {'status': 'ok', 'result': 'shadowed'}) == 'shadowed'  # A custom tool wins.
    assert tool_facts('read_file', {'status': 'error', 'error': 'File is not readable'}) is None
    assert tool_facts('record_lookup', {'status': 'error', 'error': 'rule_refused', 'ruleFeedback': {}}) is None
    assert tool_facts('future_builtin', {'status': 'ok', 'size': 4}) is None


# Built-in results on the wire ---------------------------------------------------------


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('answer,ok', [('The amount is EUR 125.', True), ('The amount is EUR 126.', False)])
def test_amounts_read_from_a_file_support_the_answer(provider, answer, ok, tmp_path):
    (tmp_path / 'ledger.txt').write_text('Total EUR 125\n', encoding='utf-8')
    with ConversationFixture(provider) as fx:
        read(fx, 'ledger.txt')
        fx.queue_text(answer)
        turn = workspace_turn(fx, tmp_path, VALUES)
        assert 'Total EUR 125' in fx.raw_requests[1].decode()  # The model was given the file.
    assert failure(turn) == (None if ok else 'ruleViolation')
    assert turn.result.stdout == (answer if ok else '')


@pytest.mark.parametrize('provider', PROVIDERS)
def test_file_line_numbers_are_never_facts(provider, tmp_path):
    (tmp_path / 'ledger.txt').write_text('\n'.join(['note'] * 125) + '\n', encoding='utf-8')
    with ConversationFixture(provider) as fx:
        read(fx, 'ledger.txt')
        fx.queue_text('The amount is EUR 125.')
        turn = workspace_turn(fx, tmp_path, VALUES)
    assert failure(turn) == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('record,runs', [('REC-123', True), ('REC-124', False)])
def test_ids_read_from_a_file_are_seen(provider, record, runs, tmp_path):
    (tmp_path / 'ids.txt').write_text('REC-123\n', encoding='utf-8')
    calls = []
    with ConversationFixture(provider) as fx:
        add_lookup(fx, calls)
        read(fx, 'ids.txt')
        fx.queue_calls([{'id': 'look-1', 'name': 'record_lookup', 'arguments': json.dumps({'recordId': record})}])
        fx.queue_text('North shelf.')
        turn = workspace_turn(fx, tmp_path, IDS, message='Look up the record in ids.txt')
    assert calls == ([{'recordId': 'REC-123'}] if runs else [])
    assert failure(turn) == (None if runs else 'ruleViolation')


# Unseen ids never reach a handler ------------------------------------------------------


def id_turn(fx, arguments, *, message):
    calls = []
    add_lookup(fx, calls)
    fx.queue_calls([{'id': 'look-1', 'name': 'record_lookup', 'arguments': json.dumps(arguments)}])
    fx.queue_text('Done.')
    runtime = ProsaicRuntime(fx.runtime.config, custom_tools=fx.tools, guards=generic_guards())
    turn = runtime.turn(tagged(IDS, tools=fx.tools), message=message, policy=fx.policy)
    return turn, calls


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('length', [1024, 1025, 4096])
@pytest.mark.parametrize(
    'shape', [lambda v: {'reference': v}, lambda v: {'batches': [[v]]}], ids=['object-key', 'nested-list']
)
def test_an_unseen_long_pattern_id_never_reaches_the_handler(provider, length, shape):
    value = 'REC-' + 'x' * (length - 4)
    with ConversationFixture(provider) as fx:
        turn, calls = id_turn(fx, shape(value), message='Find a record')
        assert len(fx.raw_requests) == 1
    assert calls == [] and failure(turn) == 'ruleViolation'
    assert turn.rules_violated and turn.rules_faulted == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_seen_long_pattern_id_runs(provider):
    value = 'REC-' + 'x' * 4092
    with ConversationFixture(provider) as fx:
        turn, calls = id_turn(fx, {'reference': value}, message=f'Find {value}')
    assert calls == [{'reference': value}] and failure(turn) is None


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('value', ['', '  '])
def test_an_empty_id_never_reaches_the_handler(provider, value):
    with ConversationFixture(provider) as fx:
        turn, calls = id_turn(fx, {'recordId': value}, message='Find the north shelf record')
        assert len(fx.raw_requests) == 1
    assert calls == [] and failure(turn) == 'ruleViolation'


# Signed amounts on the wire -----------------------------------------------------------


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('answer', ['Revenue fell EUR -999M.', 'Revenue fell €-999M.', 'Revenue fell GBP −999M.'])
def test_an_unsupported_signed_amount_fails_the_turn(provider, answer):
    with ConversationFixture(provider) as fx:
        fx.queue_text(answer)
        runtime = ProsaicRuntime(fx.runtime.config, custom_tools=fx.tools, guards=generic_guards())
        turn = runtime.turn(tagged(VALUES), message='Summarise', policy=fx.policy)
    assert failure(turn) == 'ruleViolation' and turn.result.stdout == ''
