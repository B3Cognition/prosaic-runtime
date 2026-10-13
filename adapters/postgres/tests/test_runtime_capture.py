"""Actual model transport -> durable ledger; synthetic provider only."""
import json
import os
import uuid
from dataclasses import replace
import pytest
from prosaic_runtime import (ExecutionContext, EndpointConfig, RateCard, ProsaicRuntime,
                             InvocationScope, RunPolicy, CustomTool, RuntimeConfig)
from prosaic_runtime_postgres import PostgresRecorder
from test_runtime import server, artifact, runtime


@pytest.fixture
def ledger():
    dsn=os.environ.get('METERING_TEST_DSN')
    if not dsn:
        pytest.skip('explicit disposable PostgreSQL DSN required')
    recorder=PostgresRecorder(dsn,namespace=uuid.uuid4().hex,environment='test',
        rate_card=RateCard('synthetic-v1','actual','2','8','0.5'))
    recorder.initialize()
    return recorder


def response():
    return {'model':'actual','id':'provider-test',
        'usage':{'prompt_tokens':1000,'completion_tokens':200,'total_tokens':1200,
                 'prompt_tokens_details':{'cached_tokens':400}},
        'choices':[{'message':{'content':'{"approved":true}'},'finish_reason':'stop'}]}


def failing_observer(record):
    raise RuntimeError('PRIVATE_OBSERVER_FAILURE')


@pytest.mark.parametrize('streaming',[False,True])
def test_network_to_durable_scoped_estimate(server,ledger,streaming):
    url,requests,responses=server
    if streaming:
        item=response()
        chunks=[{'model':item['model'],'usage':None,'choices':[{'delta':{'content':'ok'},'finish_reason':'stop'}]},
                {'id':item['id'],'usage':item['usage'],'choices':[]}]
        responses.append(('text/event-stream',(''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()))
    else:
        responses.append(('application/json',json.dumps(response()).encode()))
    result=runtime(EndpointConfig(url,'alias',features={'streaming':streaming}),accounting=ledger).run(
        artifact(),'private input',context=ExecutionContext(application_id='app',tenant_id='acme',billing_account_id='payer'),
        observer=failing_observer, operation_context=InvocationScope('attempt', run_id='observed-run'))
    assert result.exit_code==0
    report=ledger.report(tenant_id='acme',billing_account_id='payer')
    assert report['total_calls']==report['complete_calls']==1
    assert report['estimated_amounts']=={'USD':'0.003000000000000000'}
    assert not report['billing_eligible']
    assert ledger.report(tenant_id='other')['total_calls']==0
    rows=ledger.calls()
    assert 'private input' not in json.dumps(rows,default=str)
    restarted=PostgresRecorder(ledger._dsn,namespace=ledger.namespace,environment=ledger.environment)
    restarted.observe(rows[0]['intent']['provider_call_id'],rows[0]['observation'])
    assert restarted.report()['total_calls']==1
    assert 'payer' not in json.dumps(requests)
    assert 'PRIVATE_OBSERVER_FAILURE' not in json.dumps(rows,default=str)


def test_token_cap_retains_real_paid_observation(server, ledger):
    url, requests, responses = server
    responses.append(('application/json', json.dumps(response()).encode()))
    result = runtime(EndpointConfig(url, 'alias', features={'streaming': False}),
                     accounting=ledger).run(artifact(), policy=RunPolicy(max_reported_tokens=1199),
        observer=failing_observer, operation_context=InvocationScope('capped'))
    assert result.exit_code == 1 and result.metadata['failure_reason'] == 'token_limit'
    assert result.token_usage == 1200 and len(requests) == 1
    assert ledger.report()['total_calls'] == ledger.report()['complete_calls'] == 1
    row = ledger.calls()[0]
    assert row['observation']['usage']['total_tokens'] == 1200
    assert ledger.report()['estimated_amounts'] == {'USD': '0.003000000000000000'}


def test_journal_failure_retains_real_paid_observation_without_dispatch(server, ledger):
    url, requests, responses = server
    item = response()
    item['choices'][0]['message']['tool_calls'] = [{'id': 'effect', 'type': 'function',
        'function': {'name': 'approve', 'arguments': '{}'}}]
    item['choices'][0]['finish_reason'] = 'tool_calls'
    responses.append(('application/json', json.dumps(item).encode()))
    effects = []
    tool = CustomTool('approve', 'Approve synthetic operation',
        {'type': 'object', 'properties': {}, 'additionalProperties': False},
        lambda args, context: effects.append(context.operation_key) or {'approved': True},
        'v1', with_context=True, operation_key=lambda args, context: 'business-operation')
    class BrokenJournal:
        contract_version = 'tool-journal-v1'
        identity = 'synthetic-broken-journal'
        def claim(self, operation_namespace, operation_key, signature):
            raise RuntimeError('PRIVATE_JOURNAL_FAILURE')
        def commit(self, operation_namespace, operation_key, claim_token, bounded_outcome):
            raise AssertionError('commit must not run')
    config = RuntimeConfig({'local': EndpointConfig(url, 'alias', features={'streaming': False})},
                           {'fast': 'local'}, 'local', frozenset({'approve'}))
    result = ProsaicRuntime(config, custom_tools={'approve': tool}, accounting=ledger).run(
        artifact(['approve']), policy=RunPolicy(allowed_tools=frozenset({'approve'})),
        tool_journal=BrokenJournal(), observer=failing_observer,
        operation_context=InvocationScope('journal-attempt', operation_namespace='tenant'))
    assert result.exit_code == 1 and result.metadata['failure_reason'] == 'tool_journal_failed'
    assert result.token_usage == 1200 and len(requests) == 1 and effects == []
    assert ledger.report()['total_calls'] == ledger.report()['complete_calls'] == 1
    assert ledger.calls()[0]['observation']['usage']['total_tokens'] == 1200
    assert 'PRIVATE_JOURNAL_FAILURE' not in result.stderr + json.dumps(result.metadata)


def test_harness_resume_does_not_duplicate_accounting(server,ledger,tmp_path,monkeypatch):
    from prosaic_harness import Harness, WorkflowFactory, WorkflowCatalog, WorkflowBindings, WorkflowPolicy
    url,requests,responses=server
    config=RuntimeConfig({'local':EndpointConfig(url,'alias',features={'streaming':False})},
        {'fast':'local'},'local',frozenset())
    flow=WorkflowFactory(catalog=WorkflowCatalog(agents={'author':replace(artifact(),frontmatter={
        'name':'author','description':'Synthetic metered author','model_tier':'fast'})},schemas={'result':{
        'type':'object','properties':{'approved':{'type':'boolean'}},'required':['approved'],
        'additionalProperties':False}}),bindings=WorkflowBindings(config=config),
        policy=WorkflowPolicy(allowed_agents=frozenset({'author'}),allowed_schemas=frozenset({'result'}),
            allowed_model_tiers=frozenset({'fast'}))).build({
            'version':1,'name':'metered-resume','start':'author','steps':{
                'author':{'kind':'agent','agent':'author','schema':'result','next':'approval'},
                'approval':{'kind':'pause','question':'Approve?','choices':{'approve':'done'}},
                'done':{'kind':'finish'}}})
    rt=ProsaicRuntime(config,accounting=ledger)
    responses.append(('application/json',json.dumps(response()).encode()))
    original=Harness(flow,tmp_path/'run',runtime=rt,context=ExecutionContext(tenant_id='customer'))
    state=original.run({'task':'private request'})
    assert state['status']=='waiting'
    assert ledger.report(run_id=state['run_id'])['total_calls']==1
    resumed=Harness(flow,tmp_path/'run',runtime=rt)
    assert resumed.resume(choice='approve')['status']=='completed'
    assert len(requests)==ledger.report()['total_calls']==1


def test_uninitialized_store_blocks_provider(server,ledger):
    url,requests,_=server
    # This connection points at an owned fresh test DB without accounting tables.
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    name='metering_uninitialized_'+uuid.uuid4().hex
    with psycopg.connect(ledger._dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        recorder=PostgresRecorder(make_conninfo(ledger._dsn,dbname=name),namespace='test',environment='test')
        result=runtime(EndpointConfig(url,'test'),accounting=recorder).run(artifact())
        assert result.metadata['failure_reason']=='accounting_failed'
        assert not requests
    finally:
        with psycopg.connect(ledger._dsn,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_nested_conversation_and_planner_share_budget_and_durable_lineage(server,ledger):
    from prosaic_runtime import ExecutionBudget
    from test_runtime import completion
    url,requests,responses=server
    seen=[]
    cfg=RuntimeConfig({'local':EndpointConfig(url,'actual',features={'streaming':False})},
        {'fast':'local'},'local',frozenset({'compose_preview'}))
    def compose(args,tool_context):
        seen.append(tool_context.accounting_context)
        result=ProsaicRuntime(cfg).run(artifact(),accounting=ledger,
            context=tool_context.child_context(invocation_id='nested-planner',run_id='planner-run',step_id='compose'),
            policy=RunPolicy(timeout_s=tool_context.remaining_timeout(10)))
        assert result.exit_code==0
        return {'preview':True}
    tool=CustomTool('compose_preview','Synthetic bounded planner',
        {'type':'object','additionalProperties':False},compose,'v1',with_context=True)
    responses.extend([completion('',[{'id':'compose','type':'function','function':
        {'name':'compose_preview','arguments':'{}'}}]),completion('preview'),completion('Prepared preview')])
    budget=ExecutionBudget(timeout_s=10,max_provider_requests=3,max_tool_calls=1,max_reported_tokens=21)
    with budget.activate():
        turn=ProsaicRuntime(cfg,custom_tools={'compose_preview':tool}).turn(
            replace(artifact(['compose_preview']),body='ALWAYS prepare a preview. NEVER apply it.'),message='Preview',
            accounting=ledger,context=ExecutionContext(application_id='app',tenant_id='customer',billing_account_id='payer'),
            policy=RunPolicy(allowed_tools=frozenset({'compose_preview'}),max_provider_requests=2,max_tool_calls=1))
    assert turn.result.exit_code==0 and len(requests)==3 and budget.reported_tokens==21
    rows=ledger.calls()
    assert len(rows)==3 and ledger.report(tenant_id='other')['total_calls']==0
    nested=next(row for row in rows if row['intent']['context']['run_id']=='planner-run')
    assert nested['intent']['context']['parent_invocation_id']==seen[0].invocation_id
    assert nested['intent']['context']['billing_account_id']=='payer'
    assert all(row['observation']['usage']['total_tokens']==7 for row in rows)
