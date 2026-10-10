"""Test-only fenced journal; no expiry or automatic resolution."""
import json
import sqlite3
from uuid import uuid4

from prosaic_runtime import ToolClaim


class SqliteJournal:
    contract_version = 'tool-journal-v1'
    identity = 'synthetic-journal'

    def __init__(self, path):
        self.path = str(path)
        with self.connection() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS effects '
                '(namespace TEXT, key TEXT, signature TEXT, token TEXT, outcome TEXT, '
                'PRIMARY KEY(namespace, key))')

    def connection(self):
        return sqlite3.connect(self.path, timeout=10)

    def claim(self, operation_namespace, operation_key, signature):
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT signature, token, outcome FROM effects '
                'WHERE namespace=? AND key=?', (operation_namespace, operation_key)).fetchone()
            if row is None:
                token = str(uuid4())
                connection.execute('INSERT INTO effects VALUES(?,?,?,?,NULL)',
                    (operation_namespace, operation_key, signature, token))
                return ToolClaim('new', signature, token)
            stored, token, outcome = row
            return ToolClaim('replay' if outcome is not None else 'uncertain', stored,
                             outcome=json.loads(outcome) if outcome is not None else None)

    def commit(self, operation_namespace, operation_key, claim_token, bounded_outcome):
        with self.connection() as connection:
            update = connection.execute('UPDATE effects SET outcome=? '
                'WHERE namespace=? AND key=? AND token=? AND outcome IS NULL',
                (json.dumps(bounded_outcome, sort_keys=True, separators=(',', ':'),
                            ensure_ascii=False, allow_nan=False),
                 operation_namespace, operation_key, claim_token))
            if update.rowcount != 1:
                raise ValueError('stale fencing token')

    def signature(self, namespace, key):
        with self.connection() as connection:
            return connection.execute('SELECT signature FROM effects WHERE namespace=? AND key=?',
                                      (namespace, key)).fetchone()[0]
