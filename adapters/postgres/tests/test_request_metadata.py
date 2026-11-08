"""Additive context evidence works with the unchanged recorder0.3.0 schema."""
from copy import deepcopy
import os
import uuid

import pytest

from prosaic_runtime.accounting import AccountingError, ExecutionContext, fingerprint
from prosaic_runtime_postgres import PostgresRecorder
from test_postgres import intent, observation


@pytest.fixture
def metadata_recorder():
    dsn = os.environ.get('METERING_TEST_DSN')
    if not dsn:
        pytest.fail('METERING_TEST_DSN required for accounting request metadata PostgreSQL acceptance')
    from prosaic_runtime import RateCard
    namespace = 'metadata-' + uuid.uuid4().hex
    recorder = PostgresRecorder(dsn, namespace=namespace, environment='test',
                               rate_card=RateCard('synthetic-v1', 'test-model', '2', '8', '0.5'))
    recorder.initialize()
    try:
        yield recorder, dsn
    finally:
        # Delete only the exact namespace allocated by this fixture, no schema/role changes.
        import psycopg
        with psycopg.connect(dsn) as connection:
            for table in ('prosaic_metering_observations', 'prosaic_metering_intents'):
                connection.execute(f'DELETE FROM {table} WHERE namespace=%s AND environment=%s',
                                   (namespace, 'test'))
            for table in ('prosaic_metering_observations', 'prosaic_metering_intents'):
                assert connection.execute(f'SELECT count(*) FROM {table} WHERE namespace=%s',
                                          (namespace,)).fetchone()[0] == 0


def test_labelled_reopen_replay_conflict_and_legacy_row_are_preserved(metadata_recorder):
    recorder, dsn = metadata_recorder
    old = intent(recorder, 'legacy', tenant='tenant')
    old_hash = fingerprint(old)
    labelled = intent(recorder, 'labelled', tenant='tenant')
    labelled['context'] = ExecutionContext(tenant_id='tenant', request_metadata={
        'run': 'benchmark-42', 'variant': 'baseline'}).resolve().to_dict()
    recorder.prepare(old)
    recorder.prepare(labelled)
    recorder.observe('legacy', observation('legacy'))
    recorder.observe('labelled', observation('labelled'))
    fresh = PostgresRecorder(dsn, namespace=recorder.namespace, environment=recorder.environment)
    fresh.prepare(deepcopy(labelled))
    fresh.observe('labelled', observation('labelled'))
    rows = {row['intent']['provider_call_id']: row for row in fresh.calls()}
    assert rows['labelled']['intent'] == labelled
    assert rows['legacy']['intent'] == old and fingerprint(rows['legacy']['intent']) == old_hash
    assert 'request_metadata' not in rows['legacy']['intent']['context']
    changed = deepcopy(labelled)
    changed['context']['request_metadata']['labels']['variant'] = 'other'
    with pytest.raises(AccountingError, match='conflicting'):
        fresh.prepare(changed)
    assert fresh.report(tenant_id='tenant')['complete_calls'] == 2
    assert fresh.report(tenant_id='other')['total_calls'] == 0
    assert fresh.report()['estimated_amounts'] == {'USD': '0.006000000000000000'}
    assert fresh.report()['billing_eligible'] is False
    assert 'baseline' not in str(fresh.report())
    with pytest.raises(ValueError):
        fresh.report(variant='baseline')


def test_malformed_extension_refuses_before_inserting(metadata_recorder):
    recorder, _ = metadata_recorder
    value = intent(recorder)
    value['context']['request_metadata'] = {'version': 2, 'labels': {'variant': 'secret'}}
    with pytest.raises(AccountingError) as exc:
        recorder.prepare(value)
    assert 'secret' not in str(exc.value) and recorder.report()['total_calls'] == 0
