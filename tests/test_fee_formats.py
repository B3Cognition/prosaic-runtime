"""Fee spellings must be enforced, scaled and sourced rather than silently skipped."""

import pytest

from prosaic_runtime import CustomTool, GuardContext, generic_guards
from prosaic_runtime.guards import value_in_tool_output
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for, tagged


def fee_verdict(draft, results=(), **settings):
    context = GuardContext(
        stage='final', rule_id='fees.md:1:value_in_tool_output',
        rule_text='State only supported fees.', source_file='fees.md',
        settings=settings, paths={}, user_message='', input_data=None,
        tool_results=results, tool_name=None, arguments=None, tool_result=None,
        draft=draft,
    )
    return value_in_tool_output().check('final', context)


@pytest.mark.parametrize('fee', ['1.20 EUR', '1.20 euros', '120 cents'])
def test_invented_fee_formats_are_refused(fee):
    verdict = fee_verdict('The late fee is ' + fee + ' per day.', ({'fee': 0.4, 'currency': 'EUR'},))
    assert not verdict.ok
    assert verdict.refused_values == (fee,)


@pytest.mark.parametrize('fee', ['1.20 EUR', '1.20 euros', '120 cents'])
def test_supported_fee_formats_are_accepted(fee):
    assert fee_verdict('The late fee is ' + fee + '.', ({'fee': 1.2, 'currency': 'EUR'},)).ok


@pytest.mark.parametrize('fee', ['-1.20 EUR', '-1.20 euros', '-120 cents'])
def test_fee_sign_must_match_unless_magnitude_is_explicit(fee):
    positive = ({'fee': 1.2, 'currency': 'EUR'},)
    assert not fee_verdict(fee, positive).ok
    assert fee_verdict(fee, ({'fee': -1.2, 'currency': 'EUR'},)).ok
    assert fee_verdict(fee, positive, sign='magnitude').ok


@pytest.mark.parametrize('fee', ['1.20 EUR', '1.20 euros'])
def test_suffix_currency_does_not_match_another_currency(fee):
    assert not fee_verdict(fee, ({'fee': 1.2, 'currency': 'USD'},)).ok


def test_cents_are_hundredths_not_major_units_or_x_multiples():
    assert not fee_verdict('120 cents', ({'fee': 120},)).ok
    assert not fee_verdict('120 cents', ({'ratio': '1.2x'},)).ok
    assert not fee_verdict('1.2x', ({'fee': '120 cents'},)).ok
    assert fee_verdict('40 cents', ({'fee': 0.4},)).ok


@pytest.mark.parametrize('source', ['1.20 EUR', '1.20 euros', '120 cents'])
def test_fee_text_sources_support_the_same_major_amount(source):
    assert fee_verdict('EUR 1.20', ({'fee': source},)).ok
    assert not fee_verdict('EUR 1.30', ({'fee': source},)).ok


@pytest.mark.parametrize('source', ['1.2.3 EUR', '1.2.3 euros', '120.0.0 cents'])
def test_malformed_fee_sources_cannot_support_a_clean_amount(source):
    assert not fee_verdict('EUR 1.20', ({'fee': source},)).ok


@pytest.mark.parametrize('text', ['There are 120 centipedes.', 'Compare 2 European countries.'])
def test_currency_words_are_not_prefixes_of_ordinary_words(text):
    assert fee_verdict(text).ok


@pytest.mark.parametrize('fee', ['1.20 eUrOs', '120 CeNtS'])
def test_currency_word_case_does_not_bypass_the_fee_check(fee):
    assert not fee_verdict(fee, ({'fee': 0.4, 'currency': 'EUR'},)).ok
    assert fee_verdict(fee, ({'fee': 1.2, 'currency': 'EUR'},)).ok


@pytest.mark.parametrize('fee', ['1.20 EUR', '1.20 euros', '120 cents'])
def test_markdown_fee_bullets_are_not_negative_amounts(fee):
    assert fee_verdict('- ' + fee, ({'fee': 1.2, 'currency': 'EUR'},)).ok
    assert not fee_verdict('- ' + fee, ({'fee': -1.2, 'currency': 'EUR'},)).ok


def test_unqualified_cent_text_inherits_the_source_currency():
    source = ({'fee': '120 cents', 'currency': 'USD'},)
    assert fee_verdict('1.20 USD', source).ok
    assert not fee_verdict('1.20 EUR', source).ok


@pytest.mark.parametrize('separator', [' ', '\n'])
def test_adjacent_suffix_amounts_cannot_hide_an_unsupported_fee(separator):
    draft = '1.20 EUR' + separator + '0.40 USD'
    assert not fee_verdict(draft, ({'fee': 0.4},)).ok
    assert fee_verdict(draft, ({'fee': 1.2, 'currency': 'EUR'}, {'fee': 0.4, 'currency': 'USD'})).ok


def test_a_suffix_before_a_line_break_cannot_become_a_different_prefix_source():
    source = ({'text': 'Late fee: 1.20 EUR\n30 days grace'},)
    assert fee_verdict('EUR 1.20', source).ok
    assert not fee_verdict('EUR 30', source).ok
    assert not fee_verdict('Late fee: 1.20 EUR\n30 days grace',
                           ({'fee': 0.4, 'grace_period': 30, 'currency': 'EUR'},)).ok


@pytest.mark.parametrize('source', ['120 cents USD', '120 cents EUR'])
def test_unsupported_compound_cent_sources_do_not_discard_currency(source):
    assert not fee_verdict('EUR 1.20', ({'fee': source},)).ok
    assert not fee_verdict('USD 1.20', ({'fee': source},)).ok


@pytest.mark.parametrize('draft', ['EUR 0.40 cents', '0.40 euros cents', '0.40 EUR USD'])
def test_conflicting_or_doubled_money_units_are_refused(draft):
    assert not fee_verdict(draft, ({'fee': 0.4, 'currency': 'EUR'},)).ok
    assert not fee_verdict('EUR 0.40', ({'fee': draft},)).ok


def test_adjacent_complete_prefix_amounts_keep_their_own_currency():
    source = ({'fee': 1.2, 'currency': 'EUR'}, {'fee': 0.4, 'currency': 'USD'})
    assert fee_verdict('EUR 1.20 USD 0.40', source).ok
    assert not fee_verdict('EUR 1.20 USD 0.50', source).ok


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('fee', ['1.20 EUR', '1.20 euros', '120 cents'])
def test_invented_fee_retries_to_the_supported_fee_on_both_wires(provider, fee):
    body = '- State only supported fees. <!-- enforce: value_in_tool_output(stage=final, on_violation=retry) -->\n'
    with ConversationFixture(provider) as fixture:
        fixture.tools['loan_terms'] = CustomTool(
            'loan_terms', 'Read synthetic loan terms', {'type': 'object', 'additionalProperties': False},
            lambda arguments: {'fee': 0.4, 'currency': 'EUR'}, 'loan-terms-v1',
        )
        fixture.queue_calls([{'id': 'terms-1', 'name': 'loan_terms', 'arguments': '{}'}])
        fixture.queue_text('The late fee is ' + fee + ' per day.')
        fixture.queue_text('The late fee is EUR 0.40 per day.')
        turn = runtime_for(fixture, guards=generic_guards()).turn(
            tagged(body, tools=fixture.tools), message='What is the late fee?', policy=fixture.policy,
        )
        assert len(fixture.raw_requests) == 3
        assert fee in fixture.raw_requests[2].decode()
    assert turn.result.stdout == 'The late fee is EUR 0.40 per day.'
    assert len(turn.rules_violated) == 1 and turn.rules_faulted == ()
