import json
from pathlib import Path

import pytest

import prosaic_runtime as sdk
from prosaic_runtime.accounting import MemoryRecorder
from support.conversation_server import ConversationFixture
from support.tool_journal import SqliteJournal
from test_sub_agent_admission import agent, bind


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_child_charges_shared_dispatch_and_frozen_attribution(provider):
    with ConversationFixture(provider) as fixture:
        runner, parent, policy, contracts, frame = bind(fixture.runtime)
        fixture.queue_calls([dict(id='delegate', name='review', arguments='{"task":"Check"}')])
        fixture.queue_text('{"finding":"verified"}')
        fixture.queue_text('Reviewed')
        recorder = MemoryRecorder(provider_id=provider)
        access = sdk.SubAgentAccessV1('grants-v1', lambda *_: True, lambda *_: True, lambda: True)
        turn = runner.turn(parent, message='parent-only', policy=policy,
            tool_contract_bindings=contracts, sub_agent_frame=frame, sub_agent_access=access,
            accounting=recorder, tool_journal=SqliteJournal(Path(fixture.journal_directory.name) / "effects.db"),
            context=sdk.ExecutionContext(application_id='app', tenant_id='tenant',
                billing_account_id='payer', invocation_id=frame.scope.invocation_id))
        assert turn.result.stdout == 'Reviewed', (turn.result.stderr, turn.sub_agent_proof)
        assert len(fixture.requests) == 3
        assert 'parent-only' not in json.dumps(fixture.requests[1])
        assert 'approved' in json.dumps(fixture.requests[1])
        assert 'verified' in json.dumps(fixture.requests[2])
        assert turn.sub_agent_proof['providerRequests'] == 3
        child = next(record for record in recorder.intents.values()
                     if record['context']['parent_invocation_id'] == 'parent')
        assert child['context']['billing_account_id'] == 'payer'


def test_replay_reauthorizes_after_lookup(tmp_path):
    with ConversationFixture('openai-compatible') as fixture:
        runner, parent, policy, contracts, frame = bind(fixture.runtime)
        allowed = True

        class RevokingJournal(SqliteJournal):
            def claim(self, *args):
                nonlocal allowed
                claim = super().claim(*args)
                if claim.state == 'replay':
                    allowed = False
                return claim

        journal = RevokingJournal(tmp_path / 'effects.sqlite')
        access = sdk.SubAgentAccessV1('grants-v1', lambda *_: True, lambda *_: allowed, lambda: True)
        options = dict(message='parent', policy=policy, tool_contract_bindings=contracts,
            sub_agent_frame=frame, sub_agent_access=access, accounting=MemoryRecorder(), tool_journal=journal,
            context=sdk.ExecutionContext(invocation_id=frame.scope.invocation_id))
        fixture.queue_calls([dict(id='delegate', name='review', arguments='{"task":"Check"}')])
        fixture.queue_text('{"finding":"protected"}')
        fixture.queue_text('Reviewed')
        assert runner.turn(parent, **options).result.exit_code == 0
        fixture.queue_calls([dict(id='delegate', name='review', arguments='{"task":"Check"}')])
        fixture.queue_text('Unavailable')
        second = runner.turn(parent, **options)
        assert second.result.stdout == 'Unavailable'
        assert second.sub_agent_proof['launches'] == 0
        assert len(fixture.requests) == 5
        assert 'protected' not in json.dumps(fixture.requests[-1])


def test_parent_cites_only_delivered_approved_child_observation(tmp_path):
    from dataclasses import replace
    import re

    with ConversationFixture('openai-compatible') as fixture:
        runner, parent, _, contracts, original = bind(fixture.runtime)
        read = sdk.CustomTool('read_fact', 'Read dependency record',
            {'type': 'object', 'additionalProperties': False},
            lambda _: sdk.ToolResultV1(data={'fact': 'dependency present'}, evidence=(
                sdk.EvidenceDraftV1('graph', 'revision-1', 'private-locator', '/fact', 'Graph record'),)),
            'v1', result_contract=sdk.ToolResultContractV1('v1'))
        old = runner._custom_tools['review'].handler.spec
        child = sdk.SubAgentSpecV1(old.alias, old.description, agent(('read_fact',)), 'reads-v1',
            sdk.RunPolicy(allowed_tools=frozenset({'read_fact'}), max_provider_requests=2,
                          max_tool_calls=1, max_reported_tokens=40),
            old.result_schema, reads=(sdk.SubAgentReadV1(read, lambda _: ['graph:revision-1']),),
            select_observations=lambda observations: tuple(item['id'] for item in observations))
        runner = sdk.ProsaicRuntime(runner.config, custom_tools=sdk.bind_sub_agent_tools((child,)))
        policy = sdk.RunPolicy(allowed_tools=frozenset({'review'}), max_provider_requests=4,
                               max_tool_calls=2, max_reported_tokens=100)
        frame = sdk.SubAgentFrameV1(original.scope, sdk.SubAgentPolicyV1(1, 4, 2, 100),
                                    original.access_revision, original.projections)
        contracts = replace(contracts, citations=sdk.CitationPolicyV1('optional'))
        fixture.queue_calls([dict(id='delegate', name='review', arguments='{"task":"Check"}')])
        fixture.queue_calls([dict(id='read', name='read_fact', arguments='{}')])
        fixture.queue_text('{"finding":"verified"}')

        def answer(body):
            parent_wire = json.dumps(body)
            assert 'private-locator' not in parent_wire
            parent_id = re.findall(r'ev_[0-9a-f]{64}', parent_wire)[-1]
            assert parent_id not in json.dumps(fixture.requests[2])
            return 'Confirmed [cite:' + parent_id + ']'

        def dynamic(body):
            fixture.queue_text(answer(body))
            return fixture.responses.pop(0)
        fixture.responses.append(dynamic)
        result = runner.turn(parent, message='Review', policy=policy, tool_contract_bindings=contracts,
            sub_agent_frame=frame, sub_agent_access=sdk.SubAgentAccessV1('grants-v1', lambda *_: True,
                lambda *_: True, lambda: True), tool_journal=SqliteJournal(tmp_path / 'effects.sqlite'),
            accounting=MemoryRecorder(), context=sdk.ExecutionContext(invocation_id=frame.scope.invocation_id))
        assert result.result.exit_code == 0, result.result.stderr
        assert result.result.metadata['citations_v1']['markers']
        assert result.sub_agent_proof['toolCalls'] == 2
        assert result.sub_agent_proof['receipts'][0]['sources'] == ['fact-v1', 'graph:revision-1']



def test_denied_frozen_sources_do_not_claim_the_slot(tmp_path):
    with ConversationFixture('openai-compatible') as fixture:
        runner, parent, policy, contracts, frame = bind(fixture.runtime)
        allowed = False
        claims = []

        class Journal(SqliteJournal):
            def claim(self, *args):
                claims.append(args)
                return super().claim(*args)

        options = dict(message='parent', policy=policy, tool_contract_bindings=contracts,
            sub_agent_frame=frame, accounting=MemoryRecorder(), tool_journal=Journal(tmp_path / 'effects.sqlite'),
            sub_agent_access=sdk.SubAgentAccessV1('grants-v1', lambda *_: True, lambda *_: allowed, lambda: True),
            context=sdk.ExecutionContext(invocation_id=frame.scope.invocation_id))
        fixture.queue_calls([dict(id='delegate', name='review', arguments='{"task":"Check"}')])
        fixture.queue_text('Unavailable')
        assert runner.turn(parent, **options).result.stdout == 'Unavailable'
        assert not claims
        allowed = True
        fixture.queue_calls([dict(id='delegate', name='review', arguments='{"task":"Check"}')])
        fixture.queue_text('{"finding":"verified"}')
        fixture.queue_text('Reviewed')
        assert runner.turn(parent, **options).result.stdout == 'Reviewed'
        assert len(claims) == 1
