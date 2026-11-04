"""Second code-review regressions: call indices, batch retries, settle order, entry points, id keys."""

import json
from dataclasses import replace

import pytest

from prosaic_runtime import CustomTool, GuardContext, ProsaicArtifact, Verdict
from prosaic_runtime.guards import value_in_tool_output
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged
from test_prework_safety import rejection_server  # noqa: F401  (pytest fixture)

PROVIDERS = ['openai-compatible', 'anthropic']
TOOL_STAGES = ('before_tool', 'after_tool')


def add_lookup(fx, calls):
    def lookup(arguments):
        calls.append(arguments)
        return {'shelfMark': f'QA-{arguments["recordId"]}'}

    fx.tools['shelf_lookup'] = CustomTool(
        'shelf_lookup',
        'Look up a synthetic shelf mark',
        {
            'type': 'object',
            'properties': {'recordId': {'type': 'string'}},
            'required': ['recordId'],
            'additionalProperties': False,
        },
        lookup,
        'shelf-v1',
    )


def batch(fx, *records):
    fx.queue_calls(
        [
            {'id': f'native-{n}', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': r})}
            for n, r in enumerate(records)
        ]
    )


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_refused_call_keeps_every_later_call_index_and_outcome(provider):
    """Review finding: a refusal skipped its call index, misattributing the next call's evidence."""
    guard = recorder('shelf_check', Verdict(False, refused_values=('R-9',)), stages=TOOL_STAGES)
    calls = []
    with ConversationFixture(provider) as fx:
        add_lookup(fx, calls)
        batch(fx, 'R-9', 'R-1')
        fx.queue_text('Shelf QA-R-1.')
        body = '- Only known records. <!-- enforce: shelf_check(stage=before_tool, on_violation=retry) -->\n'
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find R-1', policy=fx.policy
        )
    outcomes = turn.evidence.to_dict()['toolOutcomes']
    calls_made = [
        block for block in turn.completed_turn.to_dict()['messages'][1]['content'] if block['type'] == 'toolCall'
    ]
    assert calls == [{'recordId': 'R-1'}]
    assert [o['callIndex'] for o in outcomes] == [0, 1]
    assert [o['callId'] for o in outcomes] == [c['callId'] for c in calls_made]
    assert outcomes[0]['outcome']['error'] == 'rule_refused' and outcomes[1]['outcome']['status'] == 'ok'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_all_refusals_of_one_batch_share_the_single_retry(provider):
    """Review finding: a second after_tool refusal in the same batch stopped the turn."""
    bad = Verdict(False, refused_values=('QA-R-1',))
    guard = recorder('shelf_check', bad, bad, stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        add_lookup(fx, [])
        batch(fx, 'R-1', 'R-2')
        fx.queue_text('Nothing to report.')
        body = '- Never quote shelf marks. <!-- enforce: shelf_check(stage=after_tool, on_violation=retry) -->\n'
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find R-1 and R-2', policy=fx.policy
        )
        follow_up = fx.raw_requests[1].decode()
    assert turn.result.exit_code == 0 and follow_up.count('rule_refused') == 2


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_retry_verdict_judged_before_a_later_repair_is_rechecked(provider):
    """Review finding: R(retry) flagged the draft, F(fix) repaired it, but R's stale verdict spent the retry."""
    body = (
        '- No bold. <!-- enforce: plain_check(stage=final, on_violation=retry) -->\n'
        '- Unwrap. <!-- enforce: unwrap_check(stage=final, on_violation=fix) -->\n'
    )
    checker = recorder('plain_check', Verdict(False))  # Then ok on the repaired text.
    fixer = recorder('unwrap_check', Verdict(False, fixed='Shelf QA-104.'))
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf **QA-104**.')
        turn = runtime_for(fx, guards=[checker.guard, fixer.guard]).turn(
            tagged(body), message='Where?', policy=fx.policy
        )
        assert len(fx.raw_requests) == 1
    assert turn.result.stdout == 'Shelf QA-104.' and turn.evidence.to_dict()['providerAttempts'] == 1
    assert [c[1].draft for c in checker.calls] == ['Shelf **QA-104**.', 'Shelf QA-104.']


def test_run_reports_malformed_artifacts_with_the_existing_value_error():
    """Review finding: refusal ran before validation and leaked a raw KeyError."""
    bad = ProsaicArtifact('a.md', 'subagent', {}, 'Plain prose.', ({'content': 'no relPath'},))
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(ValueError, match='invalid bundled resource'):
            runtime_for(fx).run(bad)


def test_unified_runtime_refuses_tagged_artifacts_before_any_backend(rejection_server, tmp_path):  # noqa: F811
    """Review finding: the delegate's refusal surfaced as an uncertain execution needing recovery."""
    from test_unified_runtime import policy, runtime

    url, requests, _ = rejection_server
    unified = runtime(tmp_path, url)
    with pytest.raises(ValueError, match='enforcement'):
        unified.run(
            tagged('- Answer as JSON. <!-- enforce: strict_json(on_violation=fail) -->\n'),
            cwd=tmp_path,
            policy=policy(),
        )
    required = replace(
        unified.config, api_config=replace(unified.config.api_config, required_guards={'strict_json': 'fail'})
    )
    with pytest.raises(ValueError, match='required_guards'):
        type(unified)(required).run(
            ProsaicArtifact('plain', 'subagent', {}, 'Plain prose.'), cwd=tmp_path, policy=policy()
        )
    assert requests == [] and not (tmp_path / 'claude.calls').exists() and not (tmp_path / 'codex.calls').exists()


@pytest.mark.parametrize('key', ['playerId', 'recordIDs', 'record_ids', 'startDate', 'fiscal_year', 'orderReference'])
def test_value_guard_never_reads_id_date_year_or_reference_keys_in_any_case_style(key):
    """Review finding: camelCase id keys could vouch for a made-up amount."""
    context = GuardContext(
        stage='final',
        rule_id='a.md:1:x',
        rule_text='Only returned figures.',
        source_file='a.md',
        settings={},
        paths={},
        user_message='',
        input_data=None,
        tool_results=({key: 4242, 'fee': 1000},),
        draft='That is EUR 4242.',
    )
    assert value_in_tool_output().check('final', context).refused_values == ('EUR 4242',)


@pytest.mark.parametrize('key', ['paid', 'valid', 'identity', 'dateline', 'year2026'])
def test_value_guard_still_reads_words_that_merely_contain_those_letters(key):
    context = GuardContext(
        stage='final',
        rule_id='a.md:1:x',
        rule_text='Only returned figures.',
        source_file='a.md',
        settings={},
        paths={},
        user_message='',
        input_data=None,
        tool_results=({key: 4242},),
        draft='That is EUR 4242.',
    )
    assert value_in_tool_output().check('final', context).ok


@pytest.mark.parametrize('provider', PROVIDERS)
def test_input_data_is_private_to_each_check(provider):
    def mutate(stage, context):
        context.input_data['branch'] = 'changed'
        return Verdict(True)

    from prosaic_runtime import Guard

    seen = []
    first = Guard('mutate_check', 'v1', ('final',), mutate)
    second = Guard(
        'read_check', 'v1', ('final',), lambda stage, context: seen.append(context.input_data) or Verdict(True)
    )
    body = (
        '- First. <!-- enforce: mutate_check(on_violation=fail) -->\n'
        '- Second. <!-- enforce: read_check(on_violation=fail) -->\n'
    )
    with ConversationFixture(provider) as fx:
        fx.queue_text('Done.')
        runtime_for(fx, guards=[first, second]).turn(
            tagged(body), message='Go', input_data={'branch': 'Gull Reach'}, policy=fx.policy
        )
    assert seen == [{'branch': 'Gull Reach'}]


# Third review pass ----------------------------------------------------------------------------


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_long_refused_value_is_elided_and_never_turns_a_fix_into_a_failure(provider):
    from prosaic_runtime import generic_guards

    target = '/reports/' + 'x' * 3000
    body = '- Plain links. <!-- enforce: no_wrapped_links(on_violation=fix) -->\n'
    with ConversationFixture(provider) as fx:
        fx.queue_text(f'See **[report]({target})**.')
        turn = runtime_for(fx, guards=generic_guards()).turn(tagged(body), message='Link?', policy=fx.policy)
    assert turn.result.exit_code == 0 and turn.result.stdout == f'See [report]({target}).'


def test_refused_values_are_bounded_not_fatal():
    from prosaic_runtime.rule_hooks import MAX_REFUSED_VALUES, MAX_VALUE_CHARS, _refused

    bounded = _refused(tuple(['y' * (MAX_VALUE_CHARS + 10)] * (MAX_REFUSED_VALUES + 5)))
    assert len(bounded) == MAX_REFUSED_VALUES and bounded[0] == 'y' * MAX_VALUE_CHARS + '…'
    with pytest.raises(ValueError):
        _refused(({'not': 'a scalar'},))


@pytest.mark.parametrize('provider', PROVIDERS)
def test_rules_violated_reports_only_verdicts_that_decided_an_outcome(provider):
    body = (
        '- No bold. <!-- enforce: plain_check(stage=final, on_violation=retry) -->\n'
        '- Unwrap. <!-- enforce: unwrap_check(stage=final, on_violation=fix) -->\n'
    )
    checker = recorder('plain_check', Verdict(False))
    fixer = recorder('unwrap_check', Verdict(False, fixed='Shelf QA-104.'))
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf **QA-104**.')
        turn = runtime_for(fx, guards=[checker.guard, fixer.guard]).turn(
            tagged(body), message='Where?', policy=fx.policy
        )
    assert turn.rules_violated == ('subagents/shelf-keeper.md:2:unwrap_check',)
    assert turn.rules_fixed == turn.rules_violated and len(turn.rules_evaluated) == 2


def test_project_root_is_fixed_when_the_runtime_is_built(tmp_path, monkeypatch):
    from prosaic_runtime import ProsaicRuntime

    schema = {'type': 'object', 'properties': {'table': {'type': 'string'}}, 'additionalProperties': False}
    guard = recorder('shelf_check', settings_schema=schema, path_settings=('table',))
    for name in ('project-a', 'project-b'):
        (tmp_path / name / 'tables').mkdir(parents=True)
        (tmp_path / name / 'tables' / 'shelves.md').write_text('synthetic')
    body = '- Use the table. <!-- enforce: shelf_check(table="tables/shelves.md") -->\n'
    with ConversationFixture('openai-compatible') as fx:
        monkeypatch.chdir(tmp_path / 'project-a')
        runtime = ProsaicRuntime(fx.runtime.config, source='.prosaic', guards=[guard.guard])
        prepared = runtime.prepare_turn(tagged(body), message='Go', policy=fx.policy)
        monkeypatch.chdir(tmp_path / 'project-b')
        fx.queue_text('Done.')
        runtime.turn(prepared)
    assert guard.calls[0][1].paths == {'table': str((tmp_path / 'project-a' / 'tables' / 'shelves.md').resolve())}


def test_null_required_guards_means_none(tmp_path):
    path = tmp_path / 'prosaic-runtime.yaml'
    path.write_text(
        'profiles: {local: {base_url: "http://127.0.0.1:9/v1", model: m}}\ndefault_profile: local\nrequired_guards:\n'
    )
    from prosaic_runtime import RuntimeConfig

    assert RuntimeConfig.load(path).required_guards == {}


def test_readmission_goes_through_a_host_override_of_prepare_turn():
    from prosaic_runtime import ProsaicRuntime

    class Counting(ProsaicRuntime):
        calls = 0

        def prepare_turn(self, *args, **kwargs):
            type(self).calls += 1
            return super().prepare_turn(*args, **kwargs)

    body = '- Keep entries short. <!-- enforce: shelf_check(stage=final, on_violation=retry) -->\n'
    with ConversationFixture('openai-compatible') as fx:
        runtime = Counting(fx.runtime.config, custom_tools=fx.tools, guards=[recorder('shelf_check').guard])
        prepared = runtime.prepare_turn(tagged(body), message='Go', policy=fx.policy)
        fx.queue_text('Short.')
        runtime.turn(prepared)
    assert Counting.calls == 2


def test_guard_context_is_read_only_and_decodes_only_what_is_read():
    from prosaic_runtime.rule_types import encode_fact

    context = GuardContext(
        stage='final',
        rule_id='a.md:1:x',
        rule_text='R.',
        source_file='a.md',
        settings=encode_fact({'limit': 3}),
        paths=encode_fact({}),
        user_message='',
        input_data=encode_fact({'big': 'x' * 1000}),
        tool_results=(),
        draft='text',
    )
    assert object.__getattribute__(context, '_decoded') == {}
    assert context.settings == {'limit': 3} and context.draft == 'text'
    assert set(object.__getattribute__(context, '_decoded')) == {'settings'}
    with pytest.raises(AttributeError):
        context.draft = 'changed'


# Focused review of the third-pass fixes ---------------------------------------------------------


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('repair', [{'recordId': float('nan')}, {'recordId': {'not', 'json'}}])
def test_an_unencodable_before_tool_repair_is_a_rule_violation_not_an_escape(provider, repair):
    guard = recorder('shelf_check', Verdict(False, fixed=repair), stages=TOOL_STAGES)
    calls = []
    with ConversationFixture(provider) as fx:
        add_lookup(fx, calls)
        batch(fx, 'R-1')
        body = '- Repair ids. <!-- enforce: shelf_check(stage=before_tool, on_violation=fix) -->\n'
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find R-1', policy=fx.policy
        )
    assert calls == [] and turn.evidence.to_dict()['failureCode'] == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_no_op_fix_superseded_by_another_repair_is_not_reported(provider):
    body = (
        '- First. <!-- enforce: noop_check(stage=final, on_violation=fix) -->\n'
        '- Second. <!-- enforce: unwrap_check(stage=final, on_violation=fix) -->\n'
    )
    noop = recorder('noop_check', Verdict(False, fixed='Shelf **QA-104**.'))  # Returns the value unchanged.
    fixer = recorder('unwrap_check', Verdict(False, fixed='Shelf QA-104.'))
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf **QA-104**.')
        turn = runtime_for(fx, guards=[noop.guard, fixer.guard]).turn(tagged(body), message='Where?', policy=fx.policy)
    assert turn.result.stdout == 'Shelf QA-104.'
    assert turn.rules_violated == turn.rules_fixed == ('subagents/shelf-keeper.md:2:unwrap_check',)


def test_repairs_compare_as_json_values():
    from prosaic_runtime.rule_hooks import _json_repair, _same

    assert (
        not _same({'n': 1}, {'n': True})
        and not _same({'n': 1}, {'n': 1.0})
        and _same({'a': 1, 'b': 2}, {'b': 2, 'a': 1})
    )
    assert _same('text', 'text') and not _same('1', 1)
    assert _json_repair({'ok': [1, 'two']}) and not _json_repair({'bad': float('inf')}) and not _json_repair(['list'])


def test_cli_preflight_still_works_with_host_only_required_guards(tmp_path):
    import subprocess
    import sys

    config = tmp_path / 'runtime.yml'
    config.write_text(
        'profiles: {local: {base_url: "http://127.0.0.1:9/v1", model: m}}\ndefault_profile: local\n'
        'required_guards: {strict_json: fail}\n'
    )
    completed = subprocess.run(
        [sys.executable, '-m', 'prosaic_runtime.cli', 'preflight', '--all-tools', '--config', str(config)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['inference'] is False


@pytest.mark.parametrize('provider', PROVIDERS)
def test_an_answer_repair_with_a_lone_surrogate_is_a_rule_violation_not_an_escape(provider):
    guard = recorder('shelf_check', Verdict(False, fixed='ok \ud800'))
    with ConversationFixture(provider) as fx:
        fx.queue_text('draft')
        body = '- Repair text. <!-- enforce: shelf_check(stage=final, on_violation=fix) -->\n'
        turn = runtime_for(fx, guards=[guard.guard]).turn(tagged(body), message='Go', policy=fx.policy)
    assert turn.evidence.to_dict()['failureCode'] == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_guard_cannot_change_an_accepted_repair_after_returning_it(provider):
    from prosaic_runtime import Guard

    held = {}

    def fixer(stage, context):
        if not held:
            held['payload'] = {'status': 'ok', 'result': {'shelfMark': 'QA-104'}}
            return Verdict(False, fixed=held['payload'])
        held['payload']['result']['shelfMark'] = float('nan')  # Mutates its own returned object later.
        return Verdict(True)

    calls = []
    with ConversationFixture(provider) as fx:
        add_lookup(fx, calls)
        batch(fx, 'R-1')
        fx.queue_text('Shelf QA-104.')
        body = '- Repair results. <!-- enforce: shelf_fix(stage=after_tool, on_violation=fix) -->\n'
        guard = Guard('shelf_fix', 'v1', TOOL_STAGES, fixer, fixes=True)
        turn = runtime_for(fx, guards=[guard]).turn(tagged(body, tools=fx.tools), message='Find R-1', policy=fx.policy)
        follow_up = fx.raw_requests[1].decode()
    assert turn.result.exit_code == 0
    assert '\\"shelfMark\\": \\"QA-104\\"' in follow_up and 'NaN' not in follow_up  # Tool content is JSON in JSON.


def test_same_compares_json_keys_after_conversion():
    from prosaic_runtime.rule_hooks import _same, _text_repair

    assert _same({1: 'a', 'b': 2}, {'1': 'a', 'b': 2})
    assert not _text_repair('ok \ud800') and _text_repair('ok')


def deep(levels):
    value = 'leaf'
    for _ in range(levels):
        value = {'next': value}
    return value


@pytest.mark.parametrize('repair', [{'status': 'ok', 'result': 'a\ud800'}, {'status': 'ok', 'result': deep(70)}])
def test_tool_repairs_meet_the_turn_capture_encoder(repair):
    from prosaic_runtime.rule_hooks import _json_repair

    assert not _json_repair(repair) and _json_repair({'status': 'ok', 'result': deep(10)})


@pytest.mark.parametrize('provider', PROVIDERS)
def test_an_after_tool_repair_the_capture_would_reject_is_a_rule_violation(provider):
    guard = recorder('shelf_check', Verdict(False, fixed={'status': 'ok', 'result': 'a\ud800'}), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        add_lookup(fx, [])
        batch(fx, 'R-1')
        body = '- Repair results. <!-- enforce: shelf_check(stage=after_tool, on_violation=fix) -->\n'
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find R-1', policy=fx.policy
        )
    assert turn.evidence.to_dict()['failureCode'] == 'ruleViolation'
