"""Actual model transport -> durable ledger; synthetic provider only."""
import json
import os
import uuid
from dataclasses import replace
import pytest
from prosaic_runtime import ExecutionContext, EndpointConfig, RateCard, ProsaicRuntime
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
        artifact(),'private input',context=ExecutionContext(application_id='app',tenant_id='acme',billing_account_id='payer'))
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


def test_harness_resume_does_not_duplicate_accounting(server,ledger,tmp_path,monkeypatch):
    from test_harness import setup
    from prosaic_harness import Harness
    url,requests,responses=server
    flow=setup(tmp_path,monkeypatch,pause=True)
    config=replace(flow.config,profiles={'local':EndpointConfig(url,'alias',features={'streaming':False})})
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
