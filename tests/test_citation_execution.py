from dataclasses import replace
import json
import re
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for, tagged
from test_tool_evidence_delivery import configure


def dynamic_answer(fx, transform=lambda answer: answer):
    class Replies(list):
        def pop(self, index=0):
            value = super().pop(index)
            return value() if callable(value) else value
    fx.responses = Replies(fx.responses)
    def answer():
        found = re.search(r'ev_[0-9a-f]{64}', fx.raw_requests[-1].decode())
        text = transform(f'14 dependents [cite:{found.group(0) if found else "unknown"}]')
        fx.queue_text(text)
        return fx.responses.pop(-1)
    fx.responses[1] = answer


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_final_text_is_withheld_until_all_checks_accept(provider):
    with ConversationFixture(provider) as fx:
        calls, events = [], []
        binding = replace(configure(fx, calls), citations=api.CitationPolicyV1('required'))
        dynamic_answer(fx)
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding, on_event=events.append)
        assert result.exit_code == 0 and '[cite:ev_' in result.stdout
        sidecar = api.CitationsV1.from_dict(result.metadata['citations_v1'])
        assert sidecar.verify_answer(result.stdout)
        assert any(e['event'] == 'tool_completed' for e in events)
        assert 'PRIVATE-LOCATOR' not in json.dumps(events)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_no_repair_binding_means_no_extra_request(provider):
    with ConversationFixture(provider) as fx:
        calls, events = [], []
        binding = replace(configure(fx, calls), citations=api.CitationPolicyV1('required'))
        fx.responses.pop()
        fx.queue_text('REJECTED ANSWER [cite:invented]')
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding, on_event=events.append)
        assert result.exit_code != 0 and result.stdout == '' and result.metadata['failure_reason'] == 'citationViolation'
        assert len(fx.requests) == 2 and calls == [1] and 'REJECTED ANSWER' not in json.dumps(events)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_denied_citation_is_not_model_repair(provider):
    with ConversationFixture(provider) as fx:
        calls = []
        binding = replace(configure(fx, calls, lambda use: use.purpose != 'cite'), citations=api.CitationPolicyV1('required'))
        dynamic_answer(fx)
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding)
        assert result.exit_code != 0 and result.metadata['failure_reason'] == 'evidenceUseDenied'
        assert calls == [1] and len(fx.requests) == 2


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_citations_checked_after_final_text_fix(provider):
    with ConversationFixture(provider) as fx:
        calls = []
        binding = replace(configure(fx, calls), citations=api.CitationPolicyV1('required'))
        dynamic_answer(fx, lambda text: 'DRAFT ' + text)
        guard = api.Guard('clean', '1', ('final',), lambda stage, ctx:
            api.Verdict(False, fixed=ctx.draft.removeprefix('DRAFT ')) if ctx.draft.startswith('DRAFT ') else api.Verdict(True), fixes=True)
        agent = tagged('- ALWAYS clean prose. <!-- enforce: clean(stage=final, on_violation=fix) -->', tools=['lookup'])
        turn = runtime_for(fx, guards=[guard]).turn(agent, message='Review', policy=fx.policy, tool_contract_bindings=binding)
        assert turn.result.exit_code == 0 and not turn.result.stdout.startswith('DRAFT')
        assert api.CitationsV1.from_dict(turn.result.metadata['citations_v1']).verify_answer(turn.result.stdout)


@pytest.mark.parametrize('cap', [1, 2])
def test_citation_repair_consumes_existing_allowance(cap):
    with ConversationFixture('openai-compatible') as fx:
        fx.queue_text('missing citation')
        fx.queue_text('still missing')
        binding = api.ToolContractBindingsV1('host', '1', api.InvocationScope('attempt'),
            evidence_authorizer=api.EvidenceAuthorizerV1('sources', '1', lambda use: True), citations=api.CitationPolicyV1('required'))
        guard = api.Guard('check', '1', ('final',), lambda stage, ctx: api.Verdict(True))
        agent = tagged('- ALWAYS check. <!-- enforce: check(stage=final, on_violation=retry) -->')
        turn = runtime_for(fx, guards=[guard]).turn(agent, message='Review', policy=replace(fx.policy, max_provider_requests=cap),
            tool_contract_bindings=binding)
        assert turn.result.exit_code != 0 and len(fx.requests) == cap and not fx.tools


def test_hash_digits_are_not_domain_fact_support():
    from prosaic_runtime.guards import value_in_tool_output
    assert value_in_tool_output().version == 'value-in-tool-output-v2'
    assert value_in_tool_output(tool_result_contract='v1').version != value_in_tool_output().version
    with pytest.raises(ValueError): value_in_tool_output(tool_result_contract='v2')
    with ConversationFixture('openai-compatible') as fx:
        calls = []
        binding = configure(fx, calls)
        fx.responses.pop()
        fx.queue_text('EUR 1')  # Wrapper version=1 cannot support this amount; logical count=14 only.
        guard = value_in_tool_output(tool_result_contract='v1')
        agent = tagged('- ALWAYS report observed values. <!-- enforce: value_in_tool_output(stage=final, on_violation=fail) -->', tools=['lookup'])
        turn = runtime_for(fx, guards=[guard]).turn(agent, message='Review', policy=fx.policy, tool_contract_bindings=binding)
        assert turn.result.exit_code != 0 and turn.result.stderr == 'ruleViolation'
