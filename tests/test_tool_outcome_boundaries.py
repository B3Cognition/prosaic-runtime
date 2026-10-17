from dataclasses import replace
import json
import pytest
from prosaic_runtime import InvocationScope
from support.conversation_server import turn_fixture
from test_tool_outcomes import Journal

@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_cancellation_cannot_erase_journal_effect_uncertainty(turn_fixture,tmp_path,provider):
    with turn_fixture(provider) as fx:
        stopped=[False];calls=[]
        def handler(args,ctx):
            calls.append(1);stopped[0]=True;raise RuntimeError('PRIVATE-AFTER-EFFECT')
        fx.add_tool('lookup',handler,operation_key=lambda a,c:'operation')
        fx.queue_calls([dict(id='call1',name='lookup',arguments='{}')])
        result=fx.runtime.run(fx.agent,cwd=tmp_path,policy=fx.policy,cancelled=lambda:stopped[0],
            operation_context=InvocationScope('attempt',operation_namespace='fixture'),tool_journal=Journal())
        assert result.metadata['failure_reason']=='tool_effect_uncertain'
        assert calls==[1] and len(fx.requests)==1

@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
@pytest.mark.parametrize('kind',['invalid-result','large-result','journal-commit','tool-cap','request-cap'])
def test_uncertain_results_and_exhausted_caps_never_dispatch_again(turn_fixture,tmp_path,provider,kind):
    with turn_fixture(provider) as fx:
        calls=[];events=[]
        def handler(*args):
            calls.append(1)
            return object() if kind=='invalid-result' else 'PRIVATE'*20000 if kind=='large-result' else {}
        journal=kind=='journal-commit'
        fx.add_tool('lookup',handler,operation_key=(lambda a,c:'operation') if journal else None)
        fx.queue_calls([dict(id='call1',name='lookup',arguments='{}')]);fx.queue_text('done')
        policy=replace(fx.policy,bounded_tool_recovery=True,max_tool_calls=0 if kind=='tool-cap' else 2,
            max_provider_requests=1 if kind=='request-cap' else 4,max_reported_tokens=8192)
        extra=dict(operation_context=InvocationScope('attempt',operation_namespace='fixture'),
            tool_journal=Journal('commit')) if journal else {}
        result=fx.runtime.run(fx.agent,cwd=tmp_path,policy=policy,on_event=events.append,**extra)
        assert result.exit_code!=0 and len(fx.requests)==1
        assert len(calls)==(0 if kind=='tool-cap' else 1)
        if kind not in {'tool-cap','request-cap'}:
            outcome=next(e['tool_outcome'] for e in events if e['event']=='tool_completed')
            assert outcome['effect']=='unknown' and outcome['recovery']=='reconcile'
        assert 'PRIVATE' not in json.dumps(events)+result.stderr

@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_streamed_tool_outcomes_preserve_safe_contract(turn_fixture,tmp_path,provider):
    with turn_fixture(provider) as fx:
        fx.add_tool('lookup',lambda args:{})
        fx.queue_calls([dict(id='call1',name='lookup',arguments='{}')])
        status,_,body=fx.responses.pop();document=json.loads(body)
        if provider=='anthropic':
            from test_anthropic_stream import events,sse
            fx.responses.append(sse(events(document))[:3])
        else:
            message=document['choices'][0]['message']
            message['tool_calls'][0]['index']=0
            chunks=[dict(choices=[dict(index=0,delta=message,finish_reason=None)]),
                dict(choices=[dict(index=0,delta={},finish_reason='tool_calls')],usage=document['usage'])]
            data=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
            fx.responses.append((200,'text/event-stream',data.encode()))
        fx.queue_stream_text('done');seen=[]
        result=fx.runtime.run(fx.agent,cwd=tmp_path,policy=replace(fx.policy,bounded_tool_recovery=True),on_event=seen.append)
        assert result.exit_code==0 and len(fx.requests)==2
        assert next(e['tool_outcome']['code'] for e in seen if e['event']=='tool_completed')=='ok'
