"""Separately installed durable metering adapter. No customer charging/export."""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, localcontext
import re

from prosaic_runtime.accounting import (
    AccountingError, ExecutionContext, RateCard, ResolvedContext, canonical, fingerprint, identifier,
)

_DDL = """
CREATE TABLE IF NOT EXISTS prosaic_metering_intents (
 namespace text NOT NULL, environment text NOT NULL, provider_call_id text NOT NULL,
 payload jsonb NOT NULL, payload_hash text NOT NULL, started_at timestamptz NOT NULL,
 PRIMARY KEY(namespace, environment, provider_call_id)
);
CREATE TABLE IF NOT EXISTS prosaic_metering_observations (
 namespace text NOT NULL, environment text NOT NULL, provider_call_id text NOT NULL,
 payload jsonb NOT NULL, payload_hash text NOT NULL,
 assessment jsonb NOT NULL, amount numeric(38,18), currency text,
 measurement_state text NOT NULL CHECK (measurement_state IN ('complete','unresolved','quarantined','unsupported')),
 PRIMARY KEY(namespace, environment, provider_call_id),
 FOREIGN KEY(namespace, environment, provider_call_id)
 REFERENCES prosaic_metering_intents(namespace, environment, provider_call_id)
);
"""
_INTENT = {'version','provider_call_id','namespace','environment','context','artifact_id','artifact_sha256',
           'profile','provider_id','credential_account','requested_model','started_at','rate_card'}
_OBSERVATION = {'version','provider_call_id','finished_at','outcome','response_model','provider_request_id',
                'service_tier','usage'}
_QUANTITIES = {'input_tokens','output_tokens','total_tokens','cached_input_tokens','reasoning_output_tokens'}
_FILTERS = {'application_id','tenant_id','billing_account_id','actor_id','project_id','request_id','run_id','invocation_id'}


def _timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise ValueError('invalid timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('UTC offset required')
    return result


def _bounded(value, limit=256):
    if type(value) is not str or not value or len(value) > limit or any(ord(c)<32 for c in value):
        raise ValueError('invalid metadata')


class PostgresRecorder:
    """Commit-before-dispatch recorder; IDs are scoped to a deployment and environment.

    Reporting filters narrow configured scope; caller authorization belongs to the host.
    No mutation, revision, reconciliation or commercial export endpoint is exposed.
    """
    durable = True

    def __init__(self, dsn, *, namespace, environment, defaults=None, rate_card=None,
                 provider_id='openai-compatible', credential_account='platform',
                 connect_timeout=5, statement_timeout_ms=5000):
        self._dsn = dsn
        self.namespace, self.environment = identifier(namespace), identifier(environment)
        self.provider_id, self.credential_account = identifier(provider_id), identifier(credential_account)
        if defaults is not None and type(defaults) is not ExecutionContext:
            raise TypeError('defaults must be ExecutionContext')
        if rate_card is not None and type(rate_card) is not RateCard:
            raise TypeError('rate_card must be RateCard')
        if type(connect_timeout) is not int or not 1 <= connect_timeout <= 30:
            raise ValueError('connect_timeout must be 1..30 seconds')
        if type(statement_timeout_ms) is not int or not 1 <= statement_timeout_ms <= 30000:
            raise ValueError('statement_timeout_ms must be 1..30000')
        self.defaults, self.rate_card = defaults, rate_card
        self._connect_timeout, self._statement_timeout = connect_timeout, statement_timeout_ms

    @contextmanager
    def _connection(self, *, snapshot=False):
        try:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(self._dsn, connect_timeout=self._connect_timeout, row_factory=dict_row,
                                options=f'-c statement_timeout={self._statement_timeout} -c lock_timeout={self._statement_timeout} -c synchronous_commit=on') as conn:
                if snapshot:
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                yield conn
        except AccountingError:
            raise
        except Exception:
            # Driver errors can contain credentials, hostnames or server-side payloads.
            raise AccountingError('PostgreSQL accounting operation failed') from None

    def initialize(self):
        """Explicit schema creation using a migration/owner connection, never automatic."""
        with self._connection() as conn:
            conn.execute(_DDL)

    def check_ready(self):
        with self._connection(snapshot=True) as conn:
            conn.execute('SELECT namespace FROM prosaic_metering_intents LIMIT 0')
            conn.execute('SELECT amount FROM prosaic_metering_observations LIMIT 0')
        return True

    def _validate_intent(self, intent):
        if type(intent) is not dict or set(intent) != _INTENT or type(intent['version']) is not int or intent['version'] != 1:
            raise ValueError('invalid intent schema')
        for name in ('namespace','environment','provider_call_id','provider_id','credential_account'):
            identifier(intent[name])
        for name in ('namespace','environment','provider_id','credential_account'):
            if intent[name] != getattr(self, name):
                raise ValueError('intent storage scope mismatch')
        ResolvedContext.from_dict(intent['context'])
        for name in ('artifact_id','profile','requested_model'):
            _bounded(intent[name])
        if not re.fullmatch('[a-f0-9]{64}', intent['artifact_sha256']):
            raise ValueError('invalid artifact hash')
        _timestamp(intent['started_at'])
        if intent['rate_card'] is not None:
            RateCard(**intent['rate_card'])
        canonical(intent)

    def prepare(self, intent):
        try:
            self._validate_intent(intent)
            encoded, digest = canonical(intent), fingerprint(intent)
        except Exception:
            raise AccountingError('invalid accounting intent') from None
        with self._connection() as conn:
            conn.execute('''INSERT INTO prosaic_metering_intents
                (namespace,environment,provider_call_id,payload,payload_hash,started_at)
                VALUES (%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT DO NOTHING''',
                (self.namespace,self.environment,intent['provider_call_id'],encoded,digest,intent['started_at']))
            row = conn.execute('''SELECT payload_hash FROM prosaic_metering_intents
                WHERE namespace=%s AND environment=%s AND provider_call_id=%s''',
                (self.namespace,self.environment,intent['provider_call_id'])).fetchone()
            if row['payload_hash'] != digest:
                raise AccountingError('conflicting accounting intent')

    def _validate_observation(self, call_id, observation):
        identifier(call_id)
        if (type(observation) is not dict or not set(observation) <= _OBSERVATION or
            not {'version','provider_call_id','finished_at','outcome','usage'} <= set(observation) or
            type(observation['version']) is not int or observation['version'] != 1 or
            observation['provider_call_id'] != call_id or observation['outcome'] not in {'completed','failed','cancelled'}):
            raise ValueError('invalid observation schema')
        _timestamp(observation['finished_at'])
        for name in ('response_model','provider_request_id','service_tier'):
            if observation.get(name) is not None:
                _bounded(observation[name])
        usage = observation['usage']
        if type(usage) is not dict or set(usage) != _QUANTITIES | {'status'} or usage['status'] not in {'reported','partial','unknown','untrusted','unsupported'}:
            raise ValueError('invalid usage schema')
        for name in _QUANTITIES:
            value = usage[name]
            if value is not None and (type(value) is not int or not 0 <= value <= 2**63-1):
                raise ValueError('invalid token quantity')
        if usage['status'] == 'reported':
            inp, out, total = (usage[n] for n in ('input_tokens','output_tokens','total_tokens'))
            if inp is not None and out is not None and (inp+out > 2**63-1 or total != inp+out):
                raise ValueError('inconsistent token totals')
            for detail, parent in (('cached_input_tokens','input_tokens'),('reasoning_output_tokens','output_tokens')):
                if usage[detail] is not None and usage[parent] is not None and usage[detail] > usage[parent]:
                    raise ValueError('inconsistent token details')
        canonical(observation)

    def observe(self, call_id, observation):
        try:
            self._validate_observation(call_id, observation)
            encoded, digest = canonical(observation), fingerprint(observation)
        except Exception:
            raise AccountingError('invalid accounting observation') from None
        with self._connection() as conn:
            key = (self.namespace,self.environment,call_id)
            # Serialize writers by the existing scoped parent row; avoids check/insert races.
            intent = conn.execute('''SELECT payload FROM prosaic_metering_intents
                WHERE namespace=%s AND environment=%s AND provider_call_id=%s FOR UPDATE''',key).fetchone()
            if intent is None:
                raise AccountingError('missing accounting intent')
            existing = conn.execute('''SELECT payload_hash FROM prosaic_metering_observations
                WHERE namespace=%s AND environment=%s AND provider_call_id=%s''',key).fetchone()
            if existing:
                if existing['payload_hash'] != digest:
                    raise AccountingError('conflicting accounting observation')
                return
            payload = intent['payload']
            if _timestamp(observation['finished_at']) < _timestamp(payload['started_at']):
                raise AccountingError('observation precedes intent')
            try:
                card = payload['rate_card']
                assessment = RateCard(**card).assess(dict(observation, requested_model=payload['requested_model'])) if card else {
                    'status':'unavailable','amount':None,'currency':None,'rate_revision':None}
            except Exception:
                raise AccountingError('accounting assessment failed') from None
            assessment.update(unit_divisor=1000000, normalization_version=1, rounding='ROUND_HALF_EVEN', billing_eligible=False)
            usage = observation['usage']
            state = ('quarantined' if usage['status']=='untrusted' else 'unsupported' if usage['status']=='unsupported'
                     else 'complete' if usage['status']=='reported' and usage['input_tokens'] is not None and usage['output_tokens'] is not None
                     else 'unresolved')
            conn.execute('''INSERT INTO prosaic_metering_observations
                (namespace,environment,provider_call_id,payload,payload_hash,assessment,amount,currency,measurement_state)
                VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s)''',
                (*key,encoded,digest,canonical(assessment),assessment['amount'],assessment['currency'],state))

    def _where(self, filters):
        if set(filters) - _FILTERS - {'started_from','started_before'}:
            raise ValueError('unsupported report filter')
        clauses, params = ['i.namespace=%s','i.environment=%s'], [self.namespace,self.environment]
        for name, value in filters.items():
            if name in _FILTERS:
                identifier(value)
                clauses.append("i.payload->'context'->>%s = %s")
                params.extend((name,value))
            else:
                _timestamp(value)
                clauses.append('i.started_at >= %s' if name=='started_from' else 'i.started_at < %s')
                params.append(value)
        return ' AND '.join(clauses), params

    def _call_query(self, filters):
        where, params = self._where(filters)
        return f'''SELECT i.payload AS intent,o.payload AS observation,o.assessment,
            COALESCE(o.measurement_state,'unresolved') AS measurement_state
            FROM prosaic_metering_intents i LEFT JOIN prosaic_metering_observations o
            USING(namespace,environment,provider_call_id) WHERE {where}
            ORDER BY i.started_at,i.provider_call_id''', params

    def calls(self, *, limit=1000, offset=0, **filters):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be an integer from 1 to 1000')
        if type(offset) is not int or not 0 <= offset <= 2**63-1:
            raise ValueError('offset must be a nonnegative signed-64-bit integer')
        query, params = self._call_query(filters)
        with self._connection(snapshot=True) as conn:
            return conn.execute(query + ' LIMIT %s OFFSET %s', (*params,limit,offset)).fetchall()

    def report(self, **filters):
        query, params = self._call_query(filters)
        counts = dict.fromkeys(('complete','unresolved','quarantined','unsupported'),0)
        estimated, unavailable, total, partial = {}, 0, 0, 0
        with self._connection(snapshot=True) as conn, localcontext() as ctx:
            ctx.prec = 60
            as_of = conn.execute('SELECT transaction_timestamp() AS as_of').fetchone()['as_of'].isoformat()
            # Server-side cursor bounds client memory. Keep the snapshot transaction open
            # until iteration and cursor close finish; never aggregate separate pages.
            with conn.cursor(name='prosaic_metering_report') as cursor:
                cursor.itersize = 1000
                cursor.execute(query, params)
                for row in cursor:
                    total += 1
                    counts[row['measurement_state']] += 1
                    if row['measurement_state']=='unresolved' and row['observation'] is not None:
                        usage = row['observation']['usage']
                        partial += int(usage['status']=='partial' or any(usage[n] is not None for n in _QUANTITIES))
                    a = row['assessment']
                    if a and a['status']=='estimated':
                        estimated[a['currency']] = estimated.get(a['currency'],Decimal(0)) + Decimal(a['amount'])
                    else:
                        unavailable += 1
        return dict(version=1,namespace=self.namespace,environment=self.environment,as_of=as_of,
            snapshot='repeatable-read',total_calls=total,complete_calls=counts['complete'],
            unresolved_calls=counts['unresolved'],quarantined_calls=counts['quarantined'],
            unsupported_calls=counts['unsupported'],partial_calls=partial,
            unavailable_assessment_calls=unavailable,
            estimated_amounts={k:format(v,'.18f') for k,v in estimated.items()},known_amounts={},
            fully_costed=not unavailable and not any(counts[s] for s in ('unresolved','quarantined','unsupported')),
            billing_eligible=False,coverage=['platform-paid OpenAI-compatible text model requests'],
            excluded_resources=['paid tools','native subscriptions','BYOK','embedding','audio','image','video','batch'],
            filters=dict(filters))
