"""Consumer witness executed from outside the Runtime source tree."""
from pathlib import Path

import pytest

import prosaic_runtime as rt


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_installed_delegate_turn(provider):
    from prosaic_runtime.accounting import MemoryRecorder
    from support.conversation_server import ConversationFixture
    from support.tool_journal import SqliteJournal
    from test_sub_agent_admission import bind

    with ConversationFixture(provider) as fixture:
        runtime, parent, policy, contracts, frame = bind(fixture.runtime)
        fixture.queue_calls([dict(id='delegate', name='review', arguments='{"task":"Check"}')])
        fixture.queue_text('{"finding":"verified"}')
        fixture.queue_text('Reviewed')
        recorder = MemoryRecorder(provider_id=provider)
        result = runtime.turn(parent, message='Parent only', policy=policy,
            tool_contract_bindings=contracts, sub_agent_frame=frame,
            sub_agent_access=rt.SubAgentAccessV1('grants-v1', lambda *_: True, lambda *_: True, lambda: True),
            accounting=recorder, tool_journal=SqliteJournal(Path(fixture.journal_directory.name) / 'effects.db'),
            context=rt.ExecutionContext(invocation_id=frame.scope.invocation_id))
        assert result.result.stdout == 'Reviewed'
        assert result.sub_agent_proof['providerRequests'] == len(fixture.requests) == 3
