"""Second-review regressions: per-stage bindings, host-pinned required guards, numeric ids."""

import json
from dataclasses import replace

import pytest
from prosaic import parse_rules

from prosaic_runtime import (
    CustomTool,
    GuardContext,
    ProsaicArtifact,
    ProsaicRuntime,
    RunPolicy,
    RuntimeConfig,
    TurnAdmissionError,
    compile_rules,
    generic_guards,
)
from prosaic_runtime.guards import args_reference_seen_ids
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

BOTH = ('before_tool', 'final')
IDS = {'keys': ['recordId']}


def composed(first, second):
    """A host composing two prose files into one turn; the body is the first file."""
    rules = parse_rules(first, 'a.md')['rules'] + parse_rules(second, 'b.md')['rules']
    return ProsaicArtifact.from_inspection(
        {
            'id': 'a.md',
            'type': 'subagent',
            'frontmatter': {'tools': ['record_lookup']},
            'body': first,
            'resources': [],
            'rules': rules,
            'ruleIssues': [],
        }
    )


def add_records(fx, calls, *, integer=False):
    def lookup(arguments):
        calls.append(arguments)
        return {'recordId': 4242, 'title': 'Synthetic tide almanac'}

    kind = 'integer' if integer else 'string'
    fx.tools['record_lookup'] = CustomTool(
        'record_lookup',
        'Look up a synthetic record',
        {
            'type': 'object',
            'properties': {'recordId': {'type': kind}},
            'required': ['recordId'],
            'additionalProperties': False,
        },
        lookup,
        'records-v1',
    )


# 1. Different stages are separate bindings ---------------------------------------------------


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_one_guard_bound_at_two_stages_in_two_files_runs_at_both(provider):
    """Regression: a before_tool fix must not silently disappear behind a final fail elsewhere."""
    guard = recorder('shelf_check', stages=BOTH)
    artifact = composed(
        '- Repair ids. <!-- enforce: shelf_check(stage=before_tool, on_violation=fix) -->\n',
        '- Never leak. <!-- enforce: shelf_check(stage=final, on_violation=fail) -->\n',
    )
    with ConversationFixture(provider) as fx:
        add_records(fx, [])
        fx.queue_calls([{'id': 'native-1', 'name': 'record_lookup', 'arguments': json.dumps({'recordId': 'R-1'})}])
        fx.queue_text('Done.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(artifact, message='Find R-1', policy=fx.policy)
    assert turn.result.exit_code == 0
    assert [stage for stage, _ in guard.calls] == ['before_tool', 'final']
    assert turn.rules_evaluated == ('a.md:1:shelf_check', 'b.md:1:shelf_check')


def test_conflicts_resolve_per_stage():
    guard = recorder('shelf_check', stages=BOTH).guard
    entries = (
        parse_rules('- A. <!-- enforce: shelf_check(stage=final, on_violation=retry) -->\n', 'a.md')['rules']
        + parse_rules('- B. <!-- enforce: shelf_check(stage=final, on_violation=fail) -->\n', 'b.md')['rules']
        + parse_rules('- C. <!-- enforce: shelf_check(stage=before_tool, on_violation=fix) -->\n', 'c.md')['rules']
    )
    with pytest.warns(UserWarning, match='final'):
        ruleset = compile_rules(entries, [guard])
    assert [(b.stage, b.action, b.file) for b in ruleset.bindings] == [
        ('final', 'fail', 'b.md'),
        ('before_tool', 'fix', 'c.md'),
    ]


# 2. Host-pinned required guards --------------------------------------------------------------


def runtime_with(fx, required, guards=None):
    config = replace(fx.runtime.config, required_guards=required)
    return ProsaicRuntime(config, custom_tools=fx.tools, guards=list(guards or generic_guards()))


def refusal(fx, artifact, required, guards=None):
    with pytest.raises(TurnAdmissionError) as caught:
        runtime_with(fx, required, guards).prepare_turn(artifact, message='Go', policy=fx.policy)
    assert caught.value.code == 'invalidPolicy'
    return caught.value.detail


def ids_tag(settings='keys=["__unused__"]', stage='before_tool', action='fail'):
    return f'- Only seen ids. <!-- enforce: args_reference_seen_ids({settings}, stage={stage}, on_violation={action}) -->\n'


def test_no_op_settings_cannot_satisfy_a_pinned_required_guard():
    """Regression: keys=["__unused__"] made a required guard a no-op."""
    required = {'args_reference_seen_ids': {'action': 'fail', 'settings': IDS}}
    with ConversationFixture('openai-compatible') as fx:
        detail = refusal(fx, tagged(ids_tag()), required)
    assert detail == 'required guard args_reference_seen_ids: settings are pinned by host policy'


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_pinned_settings_are_authoritative_when_prose_omits_them(provider):
    required = {'args_reference_seen_ids': {'action': 'fail', 'settings': IDS, 'stage': 'before_tool'}}
    calls = []
    with ConversationFixture(provider) as fx:
        add_records(fx, calls)
        fx.queue_calls([{'id': 'native-1', 'name': 'record_lookup', 'arguments': json.dumps({'recordId': 'R-9'})}])
        body = '- Only seen ids. <!-- enforce: args_reference_seen_ids(stage=before_tool, on_violation=fail) -->\n'
        turn = runtime_with(fx, required).turn(tagged(body, tools=fx.tools), message='Find R-1', policy=fx.policy)
    assert calls == [] and turn.evidence.to_dict()['failureCode'] == 'ruleViolation'


@pytest.mark.parametrize(
    'tag,detail',
    [
        (ids_tag('keys=["recordId"]', stage='before_tool'), None),
        (ids_tag('keys=["recordId", "other"]'), 'settings are pinned'),
        (ids_tag('keys=["recordId"]', action='retry'), 'action weaker than host minimum'),
    ],
)
def test_prose_may_restate_pins_and_tighten_but_never_change_or_weaken(tag, detail):
    required = {'args_reference_seen_ids': {'action': 'fail', 'settings': IDS, 'stage': 'before_tool'}}
    with ConversationFixture('openai-compatible') as fx:
        if detail is None:
            runtime_with(fx, required).prepare_turn(tagged(tag), message='Go', policy=fx.policy)
        else:
            assert detail in refusal(fx, tagged(tag), required)


def test_a_pinned_stage_refuses_prose_at_another_stage():
    required = {'shelf_check': {'action': 'retry', 'stage': 'before_answer'}}
    body = '- Keep entries short. <!-- enforce: shelf_check(stage=final, on_violation=fail) -->\n'
    with ConversationFixture('openai-compatible') as fx:
        detail = refusal(fx, tagged(body), required, [recorder('shelf_check').guard])
    assert detail == 'required guard shelf_check: stage is pinned by host policy'


def test_every_binding_of_a_required_guard_must_meet_the_minimum():
    artifact = composed(
        '- A. <!-- enforce: shelf_check(stage=before_tool, on_violation=fix) -->\n',
        '- B. <!-- enforce: shelf_check(stage=final, on_violation=fail) -->\n',
    )
    with ConversationFixture('openai-compatible') as fx:
        detail = refusal(fx, artifact, {'shelf_check': 'retry'}, [recorder('shelf_check', stages=BOTH).guard])
    assert detail == 'required guard shelf_check: action weaker than host minimum'


@pytest.mark.parametrize(
    'required',
    [
        {'shelf_check': {'settings': {}}},
        {'shelf_check': {'action': 'fail', 'extra': 1}},
        {'shelf_check': {'action': 'fail', 'stage': 'sometime'}},
        {'shelf_check': {'action': 'fail', 'settings': {'a': True}}},
        {'shelf_check': {'action': 'fail', 'settings': {'a': {'nested': 1}}}},
        {'shelf_check': 7},
    ],
)
def test_required_guard_specs_are_validated(required):
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(ValueError):
            replace(fx.runtime.config, required_guards=required)


def test_pins_are_checked_against_registered_guards_at_startup():
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(ValueError, match='pinned stage'):
            runtime_with(fx, {'args_reference_seen_ids': {'action': 'fail', 'stage': 'final'}})
        with pytest.raises(ValueError, match='pinned settings'):
            runtime_with(fx, {'args_reference_seen_ids': {'action': 'fail', 'settings': {'colour': 'red'}}})


def test_required_guard_dict_form_loads_from_yaml(tmp_path):
    path = tmp_path / 'prosaic-runtime.yaml'
    path.write_text(
        'profiles: {local: {base_url: "http://127.0.0.1:9/v1", model: m}}\ndefault_profile: local\n'
        'required_guards:\n  strict_json: retry\n'
        '  args_reference_seen_ids: {action: fail, stage: before_tool, settings: {keys: [recordId]}}\n'
    )
    assert RuntimeConfig.load(path).required_guards == {
        'strict_json': 'retry',
        'args_reference_seen_ids': {'action': 'fail', 'stage': 'before_tool', 'settings': {'keys': ['recordId']}},
    }


# 3. Numeric ids --------------------------------------------------------------------------------


def ids_check(arguments, results=()):
    context = GuardContext(
        stage='before_tool',
        rule_id='a.md:1:x',
        rule_text='Only seen ids.',
        source_file='a.md',
        settings=dict(IDS),
        paths={},
        user_message='',
        input_data=None,
        tool_results=results,
        tool_name='record_lookup',
        arguments=arguments,
    )
    return args_reference_seen_ids().check('before_tool', context)


@pytest.mark.parametrize(
    'arguments,results,refused',
    [
        ({'recordId': 4242.0}, ({'recordId': 4242},), ()),
        ({'recordId': 4242}, ({'recordId': 4242.0},), ()),
        ({'recordId': 4242.0}, (), ('4242',)),
        ({'recordId': 4242.5}, ({'recordId': 4242},), ('4242.5',)),
        ({'recordId': True}, ({'recordId': 1},), ('true',)),
        ({'recordId': [4242.0, 7]}, ({'recordId': 4242},), ('7',)),
        ({'recordId': None}, (), ()),
    ],
)
def test_numeric_ids_are_read_canonically_on_both_sides(arguments, results, refused):
    verdict = ids_check(arguments, results)
    assert verdict.ok is (not refused) and verdict.refused_values == refused


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_float_id_on_an_integer_schema_cannot_bypass_the_guard(provider):
    """Regression (security review): JSON Schema accepts 4242.0 as an integer; the guard must too."""
    calls = []
    body = '- Only seen ids. <!-- enforce: args_reference_seen_ids(keys=["recordId"], stage=before_tool, on_violation=fail) -->\n'
    with ConversationFixture(provider) as fx:
        add_records(fx, calls, integer=True)
        fx.queue_calls([{'id': 'native-1', 'name': 'record_lookup', 'arguments': '{"recordId": 4242.0}'}])
        turn = runtime_for(fx, guards=generic_guards()).turn(
            tagged(body, tools=fx.tools),
            message='Find something',
            policy=RunPolicy(allowed_tools=frozenset(fx.tools), max_provider_requests=8, max_tool_calls=16),
        )
    assert calls == [] and turn.evidence.to_dict()['failureCode'] == 'ruleViolation'
