import os
from concurrent.futures import ThreadPoolExecutor
import pytest
from prosaic_runtime.accounting import AccountingError, ExecutionContext, RateCard, normalize_usage
from prosaic_runtime_postgres import PostgresRecorder

@pytest.fixture
def recorder():
    import uuid
    dsn = os.environ.get('METERING_TEST_DSN')
    if not dsn:
        pytest.skip('METERING_TEST_DSN required: real PostgreSQL integration')
    r = PostgresRecorder(dsn, namespace=uuid.uuid4().hex, environment='test',
                         rate_card=RateCard('synthetic-v1', 'test-model', '2', '8', '0.5'))
    r.initialize()
    return r

def intent(r, call='call1', tenant='default'):
    return dict(version=1, provider_call_id=call, namespace=r.namespace, environment=r.environment,
                context=ExecutionContext(tenant_id=tenant).resolve().to_dict(), artifact_id='agent',
                artifact_sha256='a'*64, profile='test', provider_id=r.provider_id,
                credential_account=r.credential_account, requested_model='test-model',
                started_at='2026-10-08T10:00:00Z', rate_card=r.rate_card.to_dict() if r.rate_card else None)

def observation(call='call1'):
    return dict(version=1, provider_call_id=call, finished_at='2026-10-08T10:00:01Z', outcome='completed',
                response_model='test-model', usage=normalize_usage(dict(prompt_tokens=1000, completion_tokens=200,
                prompt_tokens_details=dict(cached_tokens=400))))

def test_replay_conflict_and_exact_money(recorder):
    i, o = intent(recorder), observation()
    recorder.prepare(i); recorder.prepare(i)
    recorder.observe('call1', o); recorder.observe('call1', o)
    assert recorder.report()['estimated_amounts'] == {'USD': '0.003000000000000000'}
    with pytest.raises(AccountingError):
        recorder.prepare(dict(i, requested_model='other'))
    with pytest.raises(AccountingError):
        recorder.observe('call1', dict(o, response_model='other'))
    assert recorder.report()['total_calls'] == 1

def test_concurrent_identical_and_conflicting(recorder):
    i = intent(recorder)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: recorder.prepare(i), range(16)))
        list(pool.map(lambda _: recorder.observe('call1', observation()), range(16)))
    assert recorder.report()['complete_calls'] == 1
    def write(model):
        try:
            recorder.observe('call1', dict(observation(), response_model=model))
            return True
        except AccountingError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(write, ['other']*4)) == [False]*4

def test_scope_and_unresolved(recorder):
    recorder.prepare(intent(recorder, tenant='tenant-a'))
    recorder.prepare(intent(recorder, 'call2', tenant='tenant-b'))
    assert recorder.report(tenant_id='tenant-a')['unresolved_calls'] == 1
    assert recorder.calls(tenant_id='tenant-a')[0]['intent']['context']['billing_account_id'] == 'default'
    other = PostgresRecorder(recorder._dsn, namespace=recorder.namespace, environment='production')
    assert other.report()['total_calls'] == 0
    with pytest.raises(AccountingError):
        other.prepare(intent(recorder))

def test_missing_intent_mismatch_and_unknown(recorder):
    with pytest.raises(AccountingError):
        recorder.observe('call1', observation())
    recorder.prepare(intent(recorder))
    with pytest.raises(AccountingError):
        recorder.observe('call1', observation('wrong'))
    recorder.observe('call1', dict(observation(), usage=normalize_usage(None), outcome='failed'))
    result = recorder.report()
    assert result['unresolved_calls'] == 1 and not result['fully_costed']
    assert result['estimated_amounts'] == {}

def test_atomic_assessment_failure_and_frozen_card(recorder, monkeypatch):
    recorder.prepare(intent(recorder))
    original = RateCard.assess
    def fail(*args):
        raise ValueError('synthetic failure')
    monkeypatch.setattr(RateCard, 'assess', fail)
    with pytest.raises(AccountingError):
        recorder.observe('call1', observation())
    assert recorder.calls()[0]['observation'] is None
    monkeypatch.setattr(RateCard, 'assess', original)
    recorder.rate_card = RateCard('new', 'test-model', '900', '900')
    recorder.observe('call1', observation())
    assert recorder.report()['estimated_amounts']['USD'] == '0.003000000000000000'

def test_quarantine_and_filter_validation(recorder):
    recorder.prepare(intent(recorder))
    recorder.observe('call1', dict(observation(), usage=normalize_usage({'prompt_tokens': -1})))
    assert recorder.report()['quarantined_calls'] == 1
    with pytest.raises(ValueError):
        recorder.calls(namespace='different')

def test_failure_redacts_dsn():
    r = PostgresRecorder('postgresql://user:secret@127.0.0.1:1/nope', namespace='test', environment='test')
    with pytest.raises(AccountingError) as error:
        r.check_ready()
    assert 'secret' not in str(error.value)

def test_database_rollback_and_restart(recorder):
    import psycopg
    from psycopg import sql
    original_intent = intent(recorder)
    recorder.prepare(original_intent)
    name = 'reject_' + recorder.namespace
    with psycopg.connect(recorder._dsn) as conn:
        conn.execute(sql.SQL('''CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.namespace = '{}' THEN RAISE EXCEPTION 'injected write failure'; END IF;
            RETURN NEW; END $$''').format(sql.Identifier(name), sql.SQL(recorder.namespace)))
        conn.execute(sql.SQL('CREATE TRIGGER {} BEFORE INSERT ON prosaic_metering_observations FOR EACH ROW EXECUTE FUNCTION {}()').format(sql.Identifier(name),sql.Identifier(name)))
    try:
        with pytest.raises(AccountingError):
            recorder.observe('call1', observation())
        assert recorder.calls()[0]['observation'] is None
    finally:
        with psycopg.connect(recorder._dsn) as conn:
            conn.execute(sql.SQL('DROP TRIGGER {} ON prosaic_metering_observations').format(sql.Identifier(name)))
            conn.execute(sql.SQL('DROP FUNCTION {}()').format(sql.Identifier(name)))
    recorder.observe('call1', observation())
    restarted = PostgresRecorder(recorder._dsn, namespace=recorder.namespace, environment=recorder.environment)
    restarted.prepare(original_intent); restarted.observe('call1', observation())
    assert restarted.report()['estimated_amounts'] == {'USD': '0.003000000000000000'}

def test_no_automatic_ddl_and_no_secret_payloads(recorder):
    import uuid
    bad = intent(recorder)
    bad['prompt'] = 'private'
    with pytest.raises(AccountingError):
        recorder.prepare(bad)
    assert recorder.report()['total_calls'] == 0
    missing_db = PostgresRecorder(recorder._dsn.rsplit('/', 1)[0] + '/missing_' + uuid.uuid4().hex,
                                  namespace='test', environment='test')
    with pytest.raises(AccountingError):
        missing_db.prepare(intent(missing_db))

def test_first_observation_conflict_race(recorder):
    recorder.prepare(intent(recorder))
    def submit(model):
        try:
            recorder.observe('call1', dict(observation(), response_model=model))
            return True
        except AccountingError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ['test-model','other-model'])) == [False,True]
    assert recorder.report()['total_calls'] == 1


def test_partial_evidence_never_becomes_final_estimate(recorder):
    recorder.prepare(intent(recorder))
    o = observation()
    o['usage']['status'] = 'partial'
    recorder.observe('call1', o)
    report = recorder.report()
    assert report['partial_calls'] == 1
    assert report['unresolved_calls'] == 1
    assert report['complete_calls'] == 0
    assert report['estimated_amounts'] == {}

@pytest.mark.parametrize('kwargs', [dict(limit=0), dict(limit=True), dict(limit=1001),
                                    dict(limit=1.5), dict(offset=-1), dict(offset=True), dict(offset=1.5)])
def test_pagination_rejects_invalid_parameters(recorder, kwargs):
    with pytest.raises(ValueError):
        recorder.calls(**kwargs)


def test_bounded_pages_and_unpaginated_report(recorder):
    for call in ['call3', 'call1', 'call2']:
        recorder.prepare(intent(recorder, call))
        recorder.observe(call, observation(call))
    first = recorder.calls(limit=2)
    second = recorder.calls(limit=2, offset=2)
    assert [r['intent']['provider_call_id'] for r in first] == ['call1','call2']
    assert [r['intent']['provider_call_id'] for r in second] == ['call3']
    assert recorder.calls(limit=1, offset=3) == []
    # Calls page size must never restrict aggregate report scope.
    report = recorder.report()
    assert report['total_calls'] == 3
    assert report['complete_calls'] == 3
    assert report['estimated_amounts'] == {'USD': '0.009000000000000000'}


def test_report_does_not_fetch_all_history(recorder, monkeypatch):
    import psycopg
    recorder.prepare(intent(recorder))
    recorder.observe('call1', observation())
    def forbidden_fetchall(*args, **kwargs):
        raise AssertionError('report must stream historical rows')
    monkeypatch.setattr(psycopg.Cursor, 'fetchall', forbidden_fetchall)
    assert recorder.report()['estimated_amounts'] == {'USD': '0.003000000000000000'}
