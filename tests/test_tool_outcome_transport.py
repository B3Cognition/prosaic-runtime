from dataclasses import replace
import json
import pytest
from prosaic_runtime import CustomTool,ToolFailure,RunPolicy
from support.conversation_server import turn_fixture

@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
@pytest.mark.parametrize('kind,code,requests',[
    ('invalid','invalid_arguments',2),('denied','authorization_denied',1),
    ('controlled','controlled_failure',1),('exception','handler_error',1),('data','ok',2)])
def test_native_outcomes_and_opt_in_stop_across_provider_wires(turn_fixture,tmp_path,provider,kind,code,requests):
    with turn_fixture(provider) as fx:
        effects=[];events=[];observations=[]
        def handler(args):
            effects.append(1)
            if kind=='exception':raise RuntimeError('PRIVATE-CALLBACK-SECRET')
            if kind=='controlled':return ToolFailure('invalid_arguments')
            return {'status':'error','error':'untrusted-dictionary'}
        fx.add_tool('lookup',handler,authorize=(lambda args:False) if kind=='denied' else None)
        fx.queue_calls([dict(id='call1',name='lookup',arguments='{"extra":"PRIVATE-ARG"}' if kind=='invalid' else '{}')])
        fx.queue_text('done')
        policy=replace(fx.policy,bounded_tool_recovery=True,max_provider_requests=4,max_tool_calls=2,max_reported_tokens=8192)
        result=fx.runtime.run(fx.agent,cwd=tmp_path,policy=policy,on_event=events.append,observer=observations.append)
        assert len(fx.requests)==requests and len(effects)==(0 if kind in {'invalid','denied'} else 1)
        completed=[e for e in events if e['event']=='tool_completed'];assert len(completed)==1
        outcome=completed[0]['tool_outcome']
        assert outcome['code']==code and completed[0]['call_id']=='call1' and completed[0]['tool_version']=='v1'
        assert result.exit_code==(0 if requests==2 else 1)
        assert any(e.get('tool_outcome')==outcome for e in observations)
        assert 'PRIVATE-CALLBACK-SECRET' not in json.dumps(events)+json.dumps(observations)+result.stderr
        assert 'PRIVATE-ARG' not in json.dumps(events)+json.dumps(observations)
        assert {'tool_outcomes_v1','bounded_tool_recovery_v1'}<=fx.runtime.capabilities

def test_legacy_failure_continues_and_model_envelope_is_unchanged(turn_fixture,tmp_path):
    with turn_fixture('openai-compatible') as fx:
        def handler(args):raise RuntimeError('secret')
        fx.add_tool('lookup',handler);fx.queue_calls([dict(id='call1',name='lookup',arguments='{}')]);fx.queue_text('done')
        result=fx.runtime.run(fx.agent,cwd=tmp_path,policy=fx.policy)
        assert result.exit_code==0 and len(fx.requests)==2
        message=next(m for m in fx.requests[1]['messages'] if m['role']=='tool')
        assert json.loads(message['content'])==dict(status='error',error='handler_error')

@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_conversation_event_owns_validated_outcome(turn_fixture,provider):
    with turn_fixture(provider) as fx:
        fx.add_tool('lookup',lambda args:ToolFailure('unavailable'))
        fx.queue_calls([dict(id='call1',name='lookup',arguments='{}')]);fx.queue_text('unavailable')
        events=[];result=fx.runtime.turn(fx.agent,message='Lookup',policy=fx.policy,on_event=events.append)
        completed=next(e for e in events if e['event']=='tool_completed')
        assert completed['tool_outcome']['code']=='controlled_failure'
        completed['tool_outcome']['recovery']='correct_arguments'
        assert result.evidence.to_dict()['toolOutcomes'][0]['outcome']==dict(status='error',error='unavailable')
