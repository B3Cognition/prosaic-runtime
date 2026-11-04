"""The four generic guards: inert until registered by a host and bound by prose."""

import json
from pathlib import Path

import pytest

from prosaic_runtime import CustomTool, GuardContext, Verdict, generic_guards
from prosaic_runtime.guards import args_reference_seen_ids, no_wrapped_links, strict_json, value_in_tool_output
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for, tagged

REPLAY = Path(__file__).resolve().parent / 'fixtures' / 'rule_admission' / 'value_in_tool_output_replay.json'


def context(stage='final', **values):
    defaults = dict(
        stage=stage,
        rule_id='a.md:1:x',
        rule_text='Synthetic rule.',
        source_file='a.md',
        settings={},
        paths={},
        user_message='',
        input_data=None,
        tool_results=(),
        tool_name=None,
        arguments=None,
        tool_result=None,
        draft=None,
    )
    return GuardContext(**{**defaults, **values})


def check(guard, stage='final', **values):
    verdict = guard.check(stage, context(stage, **values))
    assert type(verdict) is Verdict
    return verdict


def test_generic_guards_are_four_distinct_registrations_and_inert_until_registered():
    names = [guard.name for guard in generic_guards()]
    assert names == ['no_wrapped_links', 'value_in_tool_output', 'args_reference_seen_ids', 'strict_json']
    assert args_reference_seen_ids().stages == ('before_tool',)
    assert strict_json().stages == ('before_answer', 'final')


@pytest.mark.parametrize(
    'text,fixed',
    [
        ('See **[the shelf list](/shelves)** now.', 'See [the shelf list](/shelves) now.'),
        ('See __[a](/b)__ and *[c](/d)* and _[e](/f)_.', 'See [a](/b) and [c](/d) and [e](/f).'),
        ('Use `[a](/b)`.', 'Use [a](/b).'),
    ],
)
def test_no_wrapped_links_unwraps_complete_links(text, fixed):
    verdict = check(no_wrapped_links(), draft=text)
    assert not verdict.ok and verdict.fixed == fixed and verdict.refused_values


def test_no_wrapped_links_unwraps_nesting_one_layer_per_pass_within_the_fix_bound():
    text, passes = '***[a](/b)***', 0
    while not (verdict := check(no_wrapped_links(), draft=text)).ok:
        text, passes = verdict.fixed, passes + 1
    assert text == '[a](/b)' and 1 <= passes <= 3


@pytest.mark.parametrize(
    'text',
    [
        '[a](/b)',
        'https://example.invalid/x',
        '<https://example.invalid/x>',
        'See [a][ref].\n\n[ref]: /b',
        '**bold** then [a](/b)',
        '[**a**](/b)',
    ],
)
def test_no_wrapped_links_leaves_out_of_scope_forms_alone(text):
    assert check(no_wrapped_links(), draft=text).ok


@pytest.mark.parametrize('case', json.loads(REPLAY.read_text(encoding='utf-8'))['cases'], ids=lambda c: c['name'])
def test_value_in_tool_output_replay(case):
    results = tuple(json.loads(REPLAY.read_text(encoding='utf-8'))['toolResults'])
    verdict = check(value_in_tool_output(), draft=case['answer'], tool_results=results)
    assert verdict.ok is case['ok']
    assert list(verdict.refused_values) == case.get('refused', [])
    assert verdict.fixed is None


def test_value_in_tool_output_currency_settings_extend_the_iso_subset():
    results = ({'amount': 12},)
    assert not check(value_in_tool_output(), draft='Pay CHF 13.', tool_results=results).ok  # In the subset.
    assert check(value_in_tool_output(), draft='Pay CR 13.', tool_results=results).ok  # Not a default marker.
    extended = check(
        value_in_tool_output(), draft='Pay CR 13 or CHF 13.', tool_results=results, settings={'currencies': ['CR']}
    )
    assert extended.refused_values == ('CR 13', 'CHF 13')  # Extends, never replaces, the defaults.
    assert check(value_in_tool_output(), draft='Pay CR 12.', tool_results=results, settings={'currencies': ['CR']}).ok


@pytest.mark.parametrize(
    'answer',
    [
        'Certified to ISO 9001 since 2019.',
        'CO2 1.5M tonnes avoided.',
        'Our TOP 5 sites.',
        'An API 3 upgrade.',
        'Runs on PHP 8.2.',
        'Covers ALL 12 branches.',
        'Standard IEC 61508 applies.',
        'Uses GPT 4 class models.',
    ],
)
def test_value_in_tool_output_does_not_read_words_or_standards_as_currencies(answer):
    verdict = check(value_in_tool_output(), draft=answer, tool_results=({'amount': 1},))
    assert verdict.ok and verdict.refused_values == ()


def test_value_in_tool_output_default_markers_are_a_vendored_iso_subset():
    from prosaic_runtime.guards.values import DEFAULT_CODES, SYMBOLS

    assert {'EUR', 'USD', 'GBP', 'JPY', 'CHF'} <= set(DEFAULT_CODES)
    assert not {'PHP', 'ALL', 'TOP', 'ISO', 'API', 'CO2'} & set(DEFAULT_CODES)
    assert all(len(code) == 3 and code.isalpha() and code.isupper() for code in DEFAULT_CODES)
    assert SYMBOLS == {'€': 'EUR', '£': 'GBP', '$': 'USD'}


def test_value_in_tool_output_without_tool_results_refuses_every_marked_figure():
    verdict = check(value_in_tool_output(), draft='EUR 5 and 2x', tool_results=())
    assert verdict.refused_values == ('EUR 5', '2x')


@pytest.mark.parametrize(
    'arguments,refused',
    [
        ({'recordId': 'REC-7'}, ()),
        ({'recordId': 'REC-1'}, ()),
        ({'recordId': 'REC-3'}, ()),
        ({'recordId': 'REC-9'}, ('REC-9',)),
        ({'recordId': 'REC-10'}, ('REC-10',)),
        ({'filters': {'recordId': ['REC-1', 'REC-9']}}, ('REC-9',)),
        ({'note': 'REC-9'}, ()),
    ],
)
def test_args_reference_seen_ids_uses_tool_results_user_message_and_input_data(arguments, refused):
    verdict = check(
        args_reference_seen_ids(),
        'before_tool',
        arguments=arguments,
        tool_name='lookup',
        settings={'keys': ['recordId']},
        user_message='Find REC-1 please',
        input_data={'focus': 'REC-3'},
        tool_results=({'items': [{'recordId': 'REC-7'}]},),
    )
    assert verdict.ok is (not refused) and verdict.refused_values == refused and verdict.fixed is None


def test_args_reference_seen_ids_pattern_recognises_id_shaped_values_under_any_key():
    verdict = check(
        args_reference_seen_ids(),
        'before_tool',
        arguments={'note': 'REC-9', 'size': 'large'},
        tool_name='lookup',
        settings={'keys': ['recordId'], 'pattern': 'REC-*'},
        user_message='',
    )
    assert verdict.refused_values == ('REC-9',)


@pytest.mark.parametrize(
    'text,ok',
    [
        ('{"shelf": "QA-104"}', True),
        (' [1, 2] ', True),
        ('{"a": 1, "a": 2}', False),
        ('NaN', False),
        ('```json\n{}\n```', False),
        ('', False),
        ('Shelf QA-104', False),
    ],
)
def test_strict_json(text, ok):
    assert check(strict_json(), draft=text).ok is ok


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_value_in_tool_output_end_to_end_retry_on_the_wire(provider):
    results = json.loads(REPLAY.read_text(encoding='utf-8'))['toolResults']
    body = (
        '- State only figures the lookup returned. '
        '<!-- enforce: value_in_tool_output(stage=final, on_violation=retry) -->\n'
    )
    with ConversationFixture(provider) as fx:
        fx.tools['programme_lookup'] = CustomTool(
            'programme_lookup',
            'Synthetic programme lookup',
            {'type': 'object', 'additionalProperties': False},
            lambda arguments: results[0],
            'programme-v1',
        )
        fx.queue_calls([{'id': 'native-1', 'name': 'programme_lookup', 'arguments': '{}'}])
        fx.queue_text('North Quay spent GBP 795K.')
        fx.queue_text('North Quay spent EUR 795K.')
        turn = runtime_for(fx, guards=generic_guards()).turn(
            tagged(body, tools=fx.tools), message='Summarise', policy=fx.policy
        )
        feedback = fx.raw_requests[2].decode()
    assert turn.result.stdout == 'North Quay spent EUR 795K.'
    assert 'GBP 795K' in feedback and 'State only figures the lookup returned.' in feedback
    assert len(turn.rules_violated) == 1
