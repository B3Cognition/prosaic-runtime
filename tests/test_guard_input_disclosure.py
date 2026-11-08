import json
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for
from test_guard_input_views import rule,views


@pytest.mark.parametrize('stage',['before_tool','after_tool','final'])
@pytest.mark.parametrize('mode',['verdict','exception'])
def test_private_guard_faults_and_verdicts_never_echo_private_values(stage,mode):
    tool_calls=[];events=[]
    def check(s,c):
        secret=c.authoritative_input_data['private']
        if mode=='exception': raise ValueError(secret)
        return api.Verdict(False,reason=secret,refused_values=(secret,),fixed={'secret':secret})
    guard=api.Guard('check_facts','1',(stage,),check,input_access='authoritativeAndModel')
    with ConversationFixture('openai-compatible') as fx:
        fx.tools['lookup']=api.CustomTool('lookup','Lookup',{'type':'object','additionalProperties':False},lambda args:tool_calls.append(args) or {},'1')
        if stage!='final': fx.queue_calls([{'id':'native-1','name':'lookup','arguments':'{}'}])
        else: fx.queue_text('Approved')
        agent=rule(stage=stage)
        if stage!='final':
            from support.rules_support import tagged
            agent=tagged(agent.body,tools=['lookup'])
        result=runtime_for(fx,guards=[guard]).turn(agent,message='Review',policy=fx.policy,on_event=events.append,**views())
        assert len(fx.raw_requests)==1
        public=json.dumps([result.result.__dict__,result.evidence.to_dict(),events])+b''.join(fx.raw_requests).decode()
    assert tool_calls==([{}] if stage=='after_tool' else []) and result.evidence.to_dict()['failureCode']=='ruleViolation'
    assert 'PRIVATE-731' not in public
