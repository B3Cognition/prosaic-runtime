"""Read-only validation of the actual metering storage contract."""
from prosaic_runtime.accounting import AccountingError

_COMMON = {'namespace': ('text', True), 'environment': ('text', True),
           'provider_call_id': ('text', True), 'payload': ('jsonb', True), 'payload_hash': ('text', True)}
COLUMNS = {
    'prosaic_metering_intents': {**_COMMON, 'started_at': ('timestamp with time zone', True)},
    'prosaic_metering_observations': {**_COMMON, 'assessment': ('jsonb', True),
        'amount': ('numeric(38,18)', False), 'currency': ('text', False), 'measurement_state': ('text', True)},
}
_KEY = 'PRIMARY KEY (namespace, environment, provider_call_id)'
CONSTRAINTS = {
    'prosaic_metering_intents': {_KEY},
    'prosaic_metering_observations': {_KEY,
        'FOREIGN KEY (namespace, environment, provider_call_id) REFERENCES prosaic_metering_intents(namespace, environment, provider_call_id)',
        "CHECK ((measurement_state = ANY (ARRAY['complete'::text, 'unresolved'::text, 'quarantined'::text, 'unsupported'::text])))"},
}


def validate_catalog(tables, columns, constraints):
    def incompatible():
        raise AccountingError('incompatible PostgreSQL accounting storage contract')
    if {row[0] for row in tables} != set(COLUMNS) or any(row[1:] != ('p', 'r') for row in tables):
        incompatible()
    actual = {table: {} for table in COLUMNS}
    for table, column, kind, notnull in columns:
        if table not in actual:
            incompatible()
        actual[table][column] = (kind, notnull)
    if actual != COLUMNS:
        incompatible()
    actual_constraints = {table: set() for table in COLUMNS}
    for table, definition, validated, kind in constraints:
        if table not in actual_constraints or not validated:
            incompatible()
        # PG18 catalogs NOT NULL separately; column nullability is checked above.
        if kind != 'n':
            actual_constraints[table].add(definition)
    if actual_constraints != CONSTRAINTS:
        incompatible()


def check_ready(connection):
    from psycopg.rows import tuple_row
    with connection.cursor(row_factory=tuple_row) as cursor:
        identifiers = []
        for table in COLUMNS:
            cursor.execute('SELECT pg_catalog.to_regclass(%s)::oid', (table,))
            identifier = cursor.fetchone()[0]
            if identifier is None:
                raise AccountingError('PostgreSQL accounting storage is uninitialized')
            identifiers.append(identifier)
        cursor.execute('''SELECT c.relname,c.relpersistence,c.relkind FROM pg_catalog.pg_class c
                          WHERE c.oid = ANY(%s::oid[])''', (identifiers,))
        tables = cursor.fetchall()
        cursor.execute('''SELECT c.relname,a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
            WHERE c.oid = ANY(%s::oid[]) AND a.attnum>0 AND NOT a.attisdropped''', (identifiers,))
        columns = cursor.fetchall()
        cursor.execute('''SELECT c.relname,pg_catalog.pg_get_constraintdef(k.oid),k.convalidated,k.contype
            FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
            WHERE c.oid = ANY(%s::oid[])''', (identifiers,))
        validate_catalog(tables, columns, cursor.fetchall())
        for table, identifier in zip(COLUMNS, identifiers):
            privileges = ('SELECT', 'INSERT', 'UPDATE') if table == 'prosaic_metering_intents' else ('SELECT', 'INSERT')
            for privilege in privileges:
                cursor.execute('SELECT pg_catalog.has_table_privilege(%s,%s)', (identifier, privilege))
                if cursor.fetchone() != (True,):
                    raise AccountingError('required PostgreSQL accounting privileges are missing')
