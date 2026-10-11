"""Reproductions from the independent whole-branch conversation review."""
from dataclasses import asdict, replace
import json

import pytest
from prosaic_runtime import (CliSandboxConfig, ConversationMessage, ExecutionContext,
    ProsaicArtifact, RunPolicy, TurnAdmissionError, TurnLimits, InvocationScope)
from prosaic_runtime.accounting import MemoryRecorder
from support.conversation_server import turn_fixture


@pytest.mark.parametrize('kind', ['text', 'tool'])
@pytest.mark.parametrize('terminal', ['none', 'doneOnly', 'finishOnly'])
def test_incomplete_openai_stream_cannot_complete_or_dispatch(turn_fixture, kind, terminal):
    with turn_fixture('openai-compatible') as fx:
        effects=[]
        fx.add_tool('lookup', lambda args: effects.append(1) or {})
        fx.streaming=True
        delta={'content':'partial'} if kind == 'text' else {'tool_calls': [{
            'index':0,'id':'call_0','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
        chunks=[{'choices':[{'index':0,'delta':delta,'finish_reason':None}]}]
        if terminal == 'finishOnly':
            chunks.append({'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if kind=='tool' else 'stop'}]})
        body=''.join(f'data: {json.dumps(c)}\n\n' for c in chunks)
        if terminal == 'doneOnly':
            body+='data: [DONE]\n\n'
        fx.responses.append((200,'text/event-stream',body.encode()))
        fx.queue_stream_text('must not be reached')
        result=fx.runtime.turn(fx.agent,message='Find',policy=fx.policy)
        assert effects == [] and len(fx.requests) == 1
        assert result.completed_turn is None and result.result.metadata['failure_reason']=='incompleteGeneration'


@pytest.mark.parametrize('finish', [None, 'length', 'content_filter', 'stop', 'private-provider-secret'])
def test_non_tool_completion_cannot_authorize_json_tool_batch(turn_fixture, finish):
    with turn_fixture('openai-compatible') as fx:
        effects=[]
        fx.add_tool('lookup',lambda args:effects.append(1) or {})
        fx.queue_calls([{'id':'call_0','name':'lookup','arguments':'{}'}])
        status,kind,body=fx.responses.pop()
        doc=json.loads(body)
        doc['choices'][0]['finish_reason']=finish
        fx.responses.append((status,kind,json.dumps(doc).encode()))
        fx.queue_text('must not be reached')
        result=fx.runtime.turn(fx.agent,message='Find',policy=fx.policy)
        assert effects==[] and len(fx.requests)==1
        assert result.completed_turn is None and result.result.metadata['failure_reason']=='incompleteGeneration'


@pytest.mark.parametrize('separate', [False, True])
@pytest.mark.parametrize('journaled', [False, True])
def test_mixed_builtin_custom_completion_uses_actual_attempt_index(turn_fixture, tmp_path, separate, journaled):
    from support.tool_journal import SqliteJournal
    with turn_fixture('openai-compatible') as fx:
        cancelled=[]
        def handler(args, context=None):
            cancelled.append(True)
            return {'effect':'lookup'}
        fx.add_tool('lookup',handler,operation_key=(lambda args, ctx:'effect') if journaled else None)
        rt=fx.runtime
        rt.config=replace(rt.config, allowed_tools=rt.config.allowed_tools | {'list_files'})
        agent=ProsaicArtifact('approved','subagent',{'tools':['list_files','lookup']},'Use approved inputs.')
        policy=replace(fx.policy,allowed_tools=frozenset({'list_files','lookup'}),read_roots=(str(tmp_path),))
        builtin={'id':'builtin','name':'list_files','arguments':json.dumps({'pattern':'**/*'})}
        custom={'id':'custom','name':'lookup','arguments':'{}'}
        if separate:
            fx.queue_calls([builtin])
            fx.queue_calls([custom])
        else:
            fx.queue_calls([builtin,custom])
        result=rt.turn(agent,message='Find',policy=policy,workspace=tmp_path,
            cancelled=lambda:bool(cancelled), operation_context=InvocationScope('mixed',operation_namespace='effects'),
            tool_journal=SqliteJournal(tmp_path/'journal.sqlite') if journaled else None)
        outcomes=result.evidence.to_dict()['toolOutcomes']
        assert [r['name'] for r in outcomes] == ['list_files','lookup']
        assert [r['callIndex'] for r in outcomes] == [0,1]
        assert len({r['callId'] for r in outcomes}) == 2
        assert outcomes[1]['outcome']['result']=={'effect':'lookup'}
        assert outcomes[1]['status']=='completed'
        if journaled:
            assert outcomes[1]['operationKey']=='effect'


def test_prepared_turn_seals_effective_cli_sandbox_policy(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        rt=fx.runtime
        rt.config=replace(rt.config,cli_sandbox=CliSandboxConfig(mode='required',runtime_roots=('/private/approved',)))
        prepared=rt.prepare_turn(fx.agent,message='Find',policy=fx.policy)
        rt.config=replace(rt.config,cli_sandbox=CliSandboxConfig(mode='off'))
        other=rt.prepare_turn(fx.agent,message='Find',policy=fx.policy)
        assert prepared.context_identity != other.context_identity
        with pytest.raises(TurnAdmissionError) as exc:
            rt.turn(prepared)
        assert exc.value.code=='bindingMismatch' and fx.requests==[]


@pytest.mark.parametrize('change', ['recorder','context'])
def test_started_observer_cannot_redirect_accounting(turn_fixture, change):
    first=ExecutionContext(application_id='app',tenant_id='original',billing_account_id='payer-one')
    second=ExecutionContext(application_id='app',tenant_id='replacement',billing_account_id='payer-two')
    original=MemoryRecorder(defaults=first)
    replacement=MemoryRecorder(defaults=second)
    with turn_fixture('openai-compatible') as fx:
        rt=fx.runtime
        rt.accounting=original if change=='recorder' else None
        rt.context_defaults=first if change=='context' else None
        def observer(event):
            if event['event']=='invocation_started':
                if change=='recorder': rt.accounting=replacement
                else: rt.context_defaults=second
        fx.queue_text('done')
        result=rt.turn(fx.agent,message='Hi',policy=fx.policy,observer=observer)
        assert result.evidence.to_dict()['accounting']['context']['tenant_id']=='original'
        if change=='recorder':
            assert len(original.intents)==1 and replacement.intents=={}


def test_oversized_alias_history_is_rejected_before_decoding(turn_fixture, monkeypatch):
    message=ConversationMessage.from_dict({'role':'user','content':[{'type':'text','text':'x'*1024}],
        'turnStatus':'failed','failureCode':'providerFailure'})
    calls=[]
    original=ConversationMessage.to_dict
    def counted(self):
        calls.append(1)
        return original(self)
    monkeypatch.setattr(ConversationMessage,'to_dict',counted)
    with turn_fixture('openai-compatible') as fx:
        with pytest.raises(TurnAdmissionError):
            fx.runtime.prepare_turn(fx.agent,message='Hi',history=(message,)*5000,
                policy=fx.policy,limits=TurnLimits(max_history_bytes=2048,max_json_nodes=512))
        assert calls==[] and fx.requests==[]


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
@pytest.mark.parametrize('streamed',[False,True])
def test_unknown_native_finish_reason_never_becomes_public_metadata(turn_fixture,provider,streamed):
    with turn_fixture(provider) as fx:
        if streamed:
            fx.queue_stream_text('partial')
            status,kind,body=fx.responses.pop()
            body=body.replace(b'"stop"',b'"private-provider-secret"').replace(b'"end_turn"',b'"private-provider-secret"')
            fx.responses.append((status,kind,body))
        else:
            fx.queue_text('partial')
            status,kind,body=fx.responses.pop()
            doc=json.loads(body)
            if provider=='anthropic': doc['stop_reason']='private-provider-secret'
            else: doc['choices'][0]['finish_reason']='private-provider-secret'
            fx.responses.append((status,kind,json.dumps(doc).encode()))
        result=fx.runtime.turn(fx.agent,message='Hi',policy=fx.policy)
        assert 'private-provider-secret' not in json.dumps(asdict(result.result))
        assert result.completed_turn is None
