"""Trusted attribution and exact accounting contracts; no database dependency."""
from dataclasses import dataclass, replace, fields
from datetime import datetime, timezone
from decimal import Decimal, localcontext, ROUND_HALF_EVEN, InvalidOperation
import hashlib
import json
import re
import uuid


class AccountingError(RuntimeError):
    """Accounting failed; never retry a provider call because of this error."""


def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('IDs must be bounded ASCII identifiers')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


ATTRIBUTION = ('application_id', 'tenant_id', 'billing_account_id')
OPTIONAL = ('actor_id', 'project_id', 'run_id', 'invocation_id', 'parent_invocation_id', 'step_id')


@dataclass(frozen=True)
class ExecutionContext:
    application_id: str | None = None
    tenant_id: str | None = None
    billing_account_id: str | None = None
    actor_id: str | None = None
    project_id: str | None = None
    request_id: str | None = None
    run_id: str | None = None
    invocation_id: str | None = None
    parent_invocation_id: str | None = None
    step_id: str | None = None

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None:
                identifier(value)

    def resolve(self, defaults=None):
        defaults = defaults or ExecutionContext()
        if type(defaults) is not ExecutionContext:
            raise TypeError('defaults must be ExecutionContext')
        values, sources = {}, []
        for name in ATTRIBUTION:
            provided, configured = getattr(self, name), getattr(defaults, name)
            if name != 'application_id' and self.application_id is not None and self.application_id != defaults.application_id:
                configured = None
            # An explicit tenant must not inherit a deployment-wide payer.
            if name == 'billing_account_id' and self.tenant_id is not None and self.tenant_id != defaults.tenant_id:
                configured = None
            values[name] = provided or configured or 'default'
            sources.append('provided' if provided else 'configured' if configured else 'default')
        values.update({name: getattr(self, name) for name in OPTIONAL})
        values['request_id'] = self.request_id or uuid.uuid4().hex
        values['invocation_id'] = self.invocation_id or uuid.uuid4().hex
        return ResolvedContext(**values, sources=tuple(sources))


@dataclass(frozen=True)
class ResolvedContext:
    application_id: str
    tenant_id: str
    billing_account_id: str
    request_id: str
    invocation_id: str
    actor_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    parent_invocation_id: str | None = None
    step_id: str | None = None
    sources: tuple[str, ...] = ('provided', 'provided', 'provided')

    def __post_init__(self):
        for name in (*ATTRIBUTION, 'request_id', 'invocation_id'):
            identifier(getattr(self, name))
        for f in fields(self):
            if f.name != 'sources' and getattr(self, f.name) is not None:
                identifier(getattr(self, f.name))
        if type(self.sources) is not tuple or len(self.sources) != 3 or any(s not in {'provided', 'configured', 'default'} for s in self.sources):
            raise ValueError('invalid attribution provenance')

    def to_dict(self):
        result = {f.name: getattr(self, f.name) for f in fields(self) if f.name != 'sources'}
        result['attribution_source'] = dict(zip(ATTRIBUTION, self.sources))
        return result

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)} - {'sources'} | {'attribution_source'}:
            raise ValueError('invalid execution context schema')
        source = value['attribution_source']
        if type(source) is not dict or set(source) != set(ATTRIBUTION):
            raise ValueError('invalid attribution provenance')
        return cls(**{k: v for k, v in value.items() if k != 'attribution_source'}, sources=tuple(source[n] for n in ATTRIBUTION))

    def child(self, *, invocation_id, run_id=None, step_id=None):
        return replace(self, invocation_id=invocation_id, parent_invocation_id=self.invocation_id,
                       run_id=run_id or self.run_id, step_id=step_id)


def resolve_context(context=None, defaults=None):
    if type(context) is ResolvedContext:
        return context
    if context is None:
        context = ExecutionContext()
    if type(context) is not ExecutionContext:
        raise TypeError('context must be ExecutionContext or ResolvedContext')
    return context.resolve(defaults)


def _quantity(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def normalize_usage(raw):
    """OpenAI text snapshot semantics; detail availability is never guessed."""
    names = ('input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens', 'reasoning_output_tokens')
    result = dict.fromkeys(names)
    result['status'] = 'unknown'
    if raw is None:
        return result
    if type(raw) is not dict:
        result['status'] = 'untrusted'
        return result
    for src, dst in (('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens'), ('total_tokens', 'total_tokens')):
        if src in raw:
            if not _quantity(raw[src]):
                result['status'] = 'untrusted'
                return result
            result[dst] = raw[src]
    for src, key, dst in (('prompt_tokens_details', 'cached_tokens', 'cached_input_tokens'),
                          ('completion_tokens_details', 'reasoning_tokens', 'reasoning_output_tokens')):
        if src in raw:
            if type(raw[src]) is not dict or (key in raw[src] and not _quantity(raw[src][key])):
                result['status'] = 'untrusted'
                return result
            result[dst] = raw[src].get(key)
            zero_only = {'audio_tokens'} if src == 'prompt_tokens_details' else {'audio_tokens', 'accepted_prediction_tokens', 'rejected_prediction_tokens'}
            extras = set(raw[src]) - {key}
            if extras - zero_only or any(type(raw[src][extra]) is not int or raw[src][extra] != 0 for extra in extras):
                result['status'] = 'unsupported'
    inp, out, total = (result[n] for n in names[:3])
    if inp is not None and out is not None:
        if inp + out > 2**63 - 1 or (total is not None and total != inp + out):
            result['status'] = 'untrusted'
            return result
        result['total_tokens'] = inp + out
    for detail, parent in (('cached_input_tokens', 'input_tokens'), ('reasoning_output_tokens', 'output_tokens')):
        if result[detail] is not None and result[parent] is not None and result[detail] > result[parent]:
            result['status'] = 'untrusted'
            return result
    known = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_tokens_details', 'completion_tokens_details'}
    if set(raw) - known or result['status'] == 'unsupported':
        result['status'] = 'unsupported'
    elif result['total_tokens'] is not None:
        result['status'] = 'reported'
    return result


def money(value):
    if type(value) is not str or len(value) > 80:
        raise ValueError('money must be a bounded decimal string')
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('invalid money') from exc
    if not amount.is_finite() or amount < 0 or amount >= Decimal('1e20') or amount.as_tuple().exponent < -18:
        raise ValueError('money outside NUMERIC(38,18)')
    return amount


@dataclass(frozen=True)
class RateCard:
    revision: str
    model: str
    input_per_million: str
    output_per_million: str
    cached_input_per_million: str | None = None
    currency: str = 'USD'

    def __post_init__(self):
        identifier(self.revision)
        if type(self.model) is not str or not self.model or len(self.model) > 256 or any(ord(c) < 32 for c in self.model):
            raise ValueError('model must be a bounded printable identifier')
        if not re.fullmatch('[A-Z]{3}', self.currency):
            raise ValueError('currency must be a three-letter code')
        money(self.input_per_million)
        money(self.output_per_million)
        if self.cached_input_per_million is not None:
            money(self.cached_input_per_million)

    def to_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def assess(self, observation):
        usage = observation['usage']
        unavailable = {'status': 'unavailable', 'amount': None, 'currency': self.currency, 'rate_revision': self.revision}
        model = observation.get('response_model')
        if model != self.model or usage['status'] != 'reported' or observation.get('service_tier') not in (None, 'default'):
            return unavailable
        inp, out, cached = (usage[n] for n in ('input_tokens', 'output_tokens', 'cached_input_tokens'))
        if inp is None or out is None:
            return unavailable
        if self.cached_input_per_million is not None and cached is None:
            return unavailable
        cached = cached or 0
        cached_rate = self.cached_input_per_million or self.input_per_million
        with localcontext() as ctx:
            ctx.prec = 50
            amount = ((Decimal(inp - cached) * money(self.input_per_million) + Decimal(cached) * money(cached_rate)
                       + Decimal(out) * money(self.output_per_million)) / Decimal(1_000_000)).quantize(Decimal('1e-18'), rounding=ROUND_HALF_EVEN)
        money(str(amount))
        return {'status': 'estimated', 'amount': str(amount), 'currency': self.currency, 'rate_revision': self.revision}


class MemoryRecorder:
    """Non-durable testing recorder. Never use for financial production records."""
    durable = False

    def __init__(self, *, namespace='test', environment='test', defaults=None, rate_card=None,
                 provider_id='openai-compatible', credential_account='platform'):
        self.namespace, self.environment = identifier(namespace), identifier(environment)
        self.defaults = defaults
        self.rate_card = rate_card
        self.provider_id, self.credential_account = identifier(provider_id), identifier(credential_account)
        self.intents, self.observations = {}, {}

    def prepare(self, intent):
        key = intent['provider_call_id']
        if key in self.intents and self.intents[key] != intent:
            raise AccountingError('conflicting intent')
        self.intents[key] = json.loads(canonical(intent))

    def observe(self, call_id, observation):
        if call_id not in self.intents:
            raise AccountingError('missing intent')
        if call_id in self.observations and self.observations[call_id] != observation:
            raise AccountingError('conflicting observation')
        self.observations[call_id] = json.loads(canonical(observation))
