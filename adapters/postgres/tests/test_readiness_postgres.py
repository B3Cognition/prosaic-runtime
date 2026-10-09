"""Destructive catalog faults require an explicitly owned disposable database."""
import os
import pytest
import psycopg
from psycopg import sql
from prosaic_runtime.accounting import AccountingError
from prosaic_runtime_postgres import PostgresRecorder


@pytest.fixture
def owned_ledger():
    if os.environ.get('PROSAIC_OWNED_DB_AUDIT') != '1':
        pytest.skip('explicit owned database audit required')
    migration = os.environ['METERING_TEST_DSN']
    runtime = os.environ['METERING_RUNTIME_DSN']
    recorder = PostgresRecorder(runtime, namespace='readiness-audit', environment='test')
    recorder.check_ready()
    return migration, runtime, recorder


@pytest.mark.parametrize('table,column', [('prosaic_metering_intents','payload'),
                                         ('prosaic_metering_observations','payload_hash')])
def test_missing_column_fails_without_repair(owned_ledger, table, column):
    migration, _, recorder = owned_ledger
    with psycopg.connect(migration) as conn:
        conn.execute(sql.SQL('ALTER TABLE {} RENAME COLUMN {} TO unavailable_column').format(
            sql.Identifier(table), sql.Identifier(column)))
    try:
        with pytest.raises(AccountingError):
            recorder.check_ready()
        with psycopg.connect(migration) as conn:
            assert conn.execute('SELECT count(*) FROM information_schema.columns WHERE table_name=%s AND column_name=%s',
                (table,column)).fetchone() == (0,)
    finally:
        with psycopg.connect(migration) as conn:
            conn.execute(sql.SQL('ALTER TABLE {} RENAME COLUMN unavailable_column TO {}').format(
                sql.Identifier(table), sql.Identifier(column)))
    assert recorder.check_ready() is True


@pytest.mark.parametrize('privilege', ['SELECT','INSERT','UPDATE'])
def test_missing_runtime_grant_fails_without_repair(owned_ledger, privilege):
    migration, runtime, recorder = owned_ledger
    with psycopg.connect(runtime) as conn:
        role = conn.execute('SELECT current_user').fetchone()[0]
    with psycopg.connect(migration) as conn:
        conn.execute(sql.SQL('REVOKE {} ON prosaic_metering_intents FROM {}').format(
            sql.SQL(privilege), sql.Identifier(role)))
    try:
        with pytest.raises(AccountingError):
            recorder.check_ready()
        with psycopg.connect(runtime) as conn:
            assert conn.execute("SELECT has_table_privilege(current_user,'prosaic_metering_intents',%s)",
                (privilege,)).fetchone() == (False,)
    finally:
        with psycopg.connect(migration) as conn:
            conn.execute(sql.SQL('GRANT {} ON prosaic_metering_intents TO {}').format(
                sql.SQL(privilege), sql.Identifier(role)))
    assert recorder.check_ready() is True


def test_missing_measurement_constraint_fails_without_repair(owned_ledger):
    migration, _, recorder = owned_ledger
    with psycopg.connect(migration) as conn:
        conn.execute('ALTER TABLE prosaic_metering_observations DROP CONSTRAINT prosaic_metering_observations_measurement_state_check')
    try:
        with pytest.raises(AccountingError):
            recorder.check_ready()
    finally:
        with psycopg.connect(migration) as conn:
            conn.execute("ALTER TABLE prosaic_metering_observations ADD CONSTRAINT prosaic_metering_observations_measurement_state_check CHECK(measurement_state IN ('complete','unresolved','quarantined','unsupported'))")
    assert recorder.check_ready() is True


def test_wrong_nullability_fails_without_repair(owned_ledger):
    migration, _, recorder = owned_ledger
    with psycopg.connect(migration) as conn:
        conn.execute('ALTER TABLE prosaic_metering_intents ALTER COLUMN payload_hash DROP NOT NULL')
    try:
        with pytest.raises(AccountingError):
            recorder.check_ready()
    finally:
        with psycopg.connect(migration) as conn:
            conn.execute('ALTER TABLE prosaic_metering_intents ALTER COLUMN payload_hash SET NOT NULL')
    assert recorder.check_ready() is True
