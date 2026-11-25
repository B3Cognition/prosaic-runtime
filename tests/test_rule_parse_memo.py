"""Core's parse and strip of one text are remembered: the same answer, without parsing the prose on every turn."""
import pytest

import prosaic
from prosaic_runtime import ProsaicArtifact, rules
from prosaic_runtime.rules import artifact_rules, strip_for_model

BODY = (
    '# Agent\n\n'
    'ALWAYS cite a source. <!-- enforce: cites(stage=before_answer, on_violation=retry) -->\n\n'
    'Use `<!-- enforce: not_a_tag() -->` only in code.\n'
)


def artifact(body=BODY, identifier='agent'):
    return ProsaicArtifact(identifier, 'subagent', {'name': identifier, 'description': 'x'}, body)


@pytest.fixture(autouse=True)
def cold():
    rules.clear_parse_memo()
    yield
    rules.clear_parse_memo()


@pytest.fixture
def core_calls(monkeypatch):
    calls = {'parse': 0, 'strip': 0}
    parse, strip = prosaic.parse_rules, prosaic.strip_tags

    def counted_parse(*args, **kwargs):
        calls['parse'] += 1
        return parse(*args, **kwargs)

    def counted_strip(*args, **kwargs):
        calls['strip'] += 1
        return strip(*args, **kwargs)

    monkeypatch.setattr(prosaic, 'parse_rules', counted_parse)
    monkeypatch.setattr(prosaic, 'strip_tags', counted_strip)
    return calls


def test_the_same_artifact_is_parsed_once(core_calls):
    first, again = artifact_rules(artifact()), artifact_rules(artifact())
    assert first == again and len(first[0]) == 1
    assert core_calls['parse'] == 1


def test_the_same_text_is_stripped_once(core_calls):
    assert strip_for_model(BODY) == strip_for_model(BODY)
    assert '<!-- enforce: cites' not in strip_for_model(BODY)
    assert core_calls['strip'] == 1
    assert artifact().render() == artifact().render()
    assert core_calls['strip'] == 1


def test_each_caller_owns_its_rules():
    entries, _ = artifact_rules(artifact())
    entries[0]['settings']['changed'] = True
    entries.clear()
    again, _ = artifact_rules(artifact())
    assert len(again) == 1 and 'changed' not in again[0]['settings']


def test_the_artifact_id_is_part_of_what_is_remembered(core_calls):
    one, _ = artifact_rules(artifact(identifier='one'))
    two, _ = artifact_rules(artifact(identifier='two'))
    assert one[0]['id'] != two[0]['id'] and core_calls['parse'] == 2


def test_results_equal_core():
    entries, issues = artifact_rules(artifact())
    parsed = prosaic.parse_rules(BODY, 'agent')
    assert entries == parsed['rules'] and issues == parsed['issues']
    assert strip_for_model(BODY) == prosaic.strip_tags(BODY)


def test_an_untagged_text_is_returned_as_the_same_object():
    plain = 'No tags here.\n'
    assert strip_for_model(plain) is plain
    in_code = ''.join(['Only `<!-- enforce: x() -->`', ' in code.\n'])
    strip_for_model('Only `<!-- enforce: x() -->` in code.\n')  # an equal text was seen before
    assert strip_for_model(in_code) is in_code


def test_a_replaced_core_function_is_not_answered_from_the_memo(monkeypatch):
    assert '<!--' not in strip_for_model(BODY).split('`')[0]
    monkeypatch.setattr(prosaic, 'strip_tags', lambda text: 'replaced')
    assert strip_for_model(BODY) == 'replaced'


def test_the_memo_is_bounded():
    for index in range(rules.MEMO_ENTRIES + 5):
        strip_for_model(f'Rule {index}. <!-- prose-only: n -->\n')
    assert rules._stripped.cache_info().currsize == rules.MEMO_ENTRIES
