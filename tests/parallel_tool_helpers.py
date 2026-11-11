"""Owned test adapter for the real coordinator and real custom-tool phases."""
from dataclasses import replace
import hashlib
import json
from threading import get_ident
import time
from prosaic_runtime import CustomTool, InvocationScope, ParallelToolPolicyV1, RunPolicy
from prosaic_runtime.budgets import InvocationBudget
from prosaic_runtime.execution_budget import reserve_tool_batch
from prosaic_runtime.tool_dispatch import (prepare_custom_tool_call, invoke_prepared_tool,
    settle_custom_tool_call, start_prepared_tool, WorkerSettlementV1)
from prosaic_runtime.tool_parallel import ParallelToolCoordinator
from prosaic_runtime.tool_outcomes import ToolOutcome
from prosaic_runtime.tools import execute_custom_tool

SCHEMA={'type':'object','properties':{'i':{'type':'integer'}},'required':['i'],'additionalProperties':False}

def tool(name, handler, *, parallel=True, **kw):
    return CustomTool(name,'Read',SCHEMA,handler,'1',with_context=True,parallel_read=parallel,**kw)

def call(index,name='lookup'):
    return dict(id='wire-'+str(index),type='function',function=dict(name=name,arguments=json.dumps({'i':index})))

class Dispatch:
    def __init__(self,tools,*,boundary=lambda:None,journal=None,event=lambda *a,**k:None,scope='invocation'):
        self.custom=tools;self.boundary=boundary;self.journal=journal;self.event=event
        self.scope=InvocationScope(scope,operation_namespace='tenant');self.batch_id=hashlib.sha256(scope.encode()).hexdigest()
        self.budget=InvocationBudget(RunPolicy(max_provider_requests=3,max_tool_calls=32))
        self.completed=[];self.callback_threads=[];self.outcomes={};self.options={};self.bounded_recovery=False
    def identity(self,c,index):return hashlib.sha256((self.scope.invocation_id+':'+str(index)).encode()).hexdigest(),index
    def prepare(self,c,*,permit,batch_index,cancelled,await_operation):
        permit.consume(self.budget);self.callback_threads.append(get_ident())
        ident,_=self.identity(c,batch_index)
        def complete(outcome,status='completed',**refs):self.completed.append((batch_index,outcome,status));self.callback_threads.append(get_ident())
        options=dict(check_boundary=self.boundary,scope=self.scope,deadline=time.monotonic()+10,cancelled=cancelled,
            tool_journal=self.journal,on_completion=complete,
            on_outcome=lambda out:self.outcomes.__setitem__(batch_index,out),
            _await_operation=await_operation,_parallel_mode=True)
        self.options[batch_index]=options
        return prepare_custom_tool_call(self.custom[c['function']['name']],c['function']['arguments'],
            call_id=ident,call_index=batch_index,batch_index=batch_index,options=options)
    def start(self,p):
        self.callback_threads.append(get_ident());start_prepared_tool(p)
        self.event('tool_started',call_id=p.call_id,name=p.tool.name)
    def settle(self,p,s):
        self.callback_threads.append(get_ident());return settle_custom_tool_call(p,s)
    def serial(self,c,*,permit,batch_index,cancelled):
        permit.consume(self.budget);self.callback_threads.append(get_ident())
        registered=self.custom[c['function']['name']];ident,_=self.identity(c,batch_index);seen=[]
        self.event('tool_started',call_id=ident,name=registered.name)
        value=execute_custom_tool(registered,c['function']['arguments'],check_boundary=self.boundary,
            scope=self.scope,deadline=time.monotonic()+10,cancelled=cancelled,tool_journal=self.journal,
            call_index=batch_index,on_outcome=seen.append,
            on_completion=lambda out,status='completed',**kw:self.completed.append((batch_index,out,status)))
        self.outcomes[batch_index]=seen[-1]
        return WorkerSettlementV1(ident,batch_index,batch_index,seen[-1].code,json.dumps(value).encode(),
            handler_entered=not seen[-1].replayed,replayed=seen[-1].replayed)
    def restore(self,c,s):pass


def coordinator(tools,calls,*,policy=None,reduce=None,**kw):
    dispatch=Dispatch(tools,**kw);reservation=reserve_tool_batch(dispatch.budget,(),len(calls))
    coord=ParallelToolCoordinator(policy or ParallelToolPolicyV1(max_concurrency=2),check_boundary=dispatch.boundary,
        reservation=reservation,dispatch=dispatch,reduce=reduce or (lambda c,s:s.outcome),emit=dispatch.event)
    return coord,dispatch
