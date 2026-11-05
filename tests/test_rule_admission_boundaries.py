"""Admission boundaries: entry-point refusal, rebuilt artifacts, resources and compile-once admission."""

import warnings

import pytest
from prosaic import parse_rules

from prosaic_runtime import NativePolicy, ProsaicArtifact, RuleBindingWarning, TurnAdmissionError
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

LIMIT_SCHEMA = {'type': 'object', 'properties': {'limit': {'type': 'integer'}}, 'additionalProperties': False}
COMPOSED = parse_rules('- Answer as JSON. <!-- enforce: strict_json(on_violation=fail) -->\n', 'subagents/included.md')


def composed_artifact():
    """Inspection rules from another composed file; this body itself is untagged."""
    return ProsaicArtifact.from_inspection(
        {
            'id': 'subagents/front.md',
            'type': 'subagent',
            'frontmatter': {},
            'body': 'Plain front prose.',
            'resources': [],
            'rules': COMPOSED['rules'],
            'ruleIssues': [],
        }
    )


def test_run_refuses_rules_from_composed_files_even_when_the_body_is_untagged():
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(ValueError, match='enforcement'):
            runtime_for(fx).run(composed_artifact())
        assert fx.raw_requests == []


def test_native_runtime_refuses_composed_rules_after_its_artifact_rebuild(tmp_path):
    from test_native_runtime import setup_runtime

    runtime = setup_runtime(tmp_path)
    with pytest.raises(ValueError, match='enforcement'):
        runtime.run(composed_artifact(), cwd=tmp_path, policy=NativePolicy())
    assert not (tmp_path / 'primary.calls').exists() and not (tmp_path / 'fallback.calls').exists()


def test_run_refuses_when_the_host_requires_guards_it_cannot_enforce():
    with ConversationFixture('openai-compatible') as fx:
        runtime = runtime_for(fx, guards=[recorder('strict_json').guard], required_guards={'strict_json': 'fail'})
        with pytest.raises(ValueError, match='required_guards'):
            runtime.run(ProsaicArtifact('plain', 'subagent', {}, 'Untagged prose.'))
        assert fx.raw_requests == []


def test_tags_in_markdown_resources_refuse_admission_and_other_files_pass_through():
    tag = '- Rule. <!-- enforce: shelf_check() -->\n'
    with ConversationFixture('openai-compatible') as fx:
        runtime = runtime_for(fx, guards=[recorder('shelf_check').guard])
        in_markdown = tagged('Plain prose.', resources=[{'relPath': 'notes.md', 'content': tag}])
        with pytest.raises(TurnAdmissionError) as caught:
            runtime.prepare_turn(in_markdown, message='Go', policy=fx.policy)
        assert caught.value.code == 'invalidArtifact'
        in_text = tagged('Plain prose.', resources=[{'relPath': 'notes.txt', 'content': tag}])
        runtime.prepare_turn(in_text, message='Go', policy=fx.policy)


def test_rules_compile_once_per_preparation_and_readmission_does_not_repeat_warnings(monkeypatch):
    import prosaic_runtime.rules as rules

    body = '- Short. <!-- enforce: shelf_check(limit=10, on_violation=retry) -->\n'
    other = parse_rules('- Short too. <!-- enforce: shelf_check(limit=10, on_violation=fail) -->\n', 'b.md')['rules']
    artifact = ProsaicArtifact.from_inspection(
        {
            'id': 'a.md',
            'type': 'subagent',
            'frontmatter': {},
            'body': body,
            'resources': [],
            'rules': parse_rules(body, 'a.md')['rules'] + other,
            'ruleIssues': [],
        }
    )
    compiles = []
    original = rules.compile_rules
    monkeypatch.setattr(rules, 'compile_rules', lambda *a, **k: compiles.append(k.get('warn')) or original(*a, **k))
    guard = recorder('shelf_check', settings_schema=LIMIT_SCHEMA)
    with ConversationFixture('openai-compatible') as fx:
        runtime = runtime_for(fx, guards=[guard.guard])
        with warnings.catch_warnings(record=True) as prepared_warnings:
            warnings.simplefilter('always')
            prepared = runtime.prepare_turn(artifact, message='Go', policy=fx.policy)
        fx.queue_text('Short entry')
        with warnings.catch_warnings(record=True) as executed_warnings:
            warnings.simplefilter('always')
            turn = runtime.turn(prepared)
    assert [w.category for w in prepared_warnings] == [RuleBindingWarning]
    assert [w.category for w in executed_warnings] == []
    assert compiles == [True, False]  # Preparation, then silent readmission; execution reuses it.
    assert turn.result.exit_code == 0 and len(guard.calls) == 1
