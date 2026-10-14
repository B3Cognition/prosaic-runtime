"""Trusted local counting implementations with immutable admitted descriptors."""
from collections.abc import Mapping
from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Protocol

from .conversation import TurnAdmissionError, encode_owned
from .context_contracts import ContextPolicy, TokenCounterDescriptor, MAX_INTEGER


class TokenCounter(Protocol):
    def count_tokens(self, request: dict[str, object]) -> int: ...


@dataclass(frozen=True)
class TokenCounterRegistration:
    descriptor: TokenCounterDescriptor
    counter: TokenCounter

    def __post_init__(self):
        if type(self.descriptor) is not TokenCounterDescriptor or not callable(getattr(self.counter, 'count_tokens', None)):
            raise TurnAdmissionError('invalidPolicy')
        object.__setattr__(self, 'descriptor', TokenCounterDescriptor.from_dict(self.descriptor.to_dict()))


class TokenCounterRegistry:
    __slots__ = ('_registrations',)

    def __init__(self, registrations):
        try:
            if not isinstance(registrations, Mapping) or len(registrations) > 256:
                raise ValueError('registry bounds')
            owned = {}
            for identity, entry in registrations.items():
                if (type(entry) is not TokenCounterRegistration or type(identity) is not str
                        or identity != entry.descriptor.identity):
                    raise ValueError('registration identity')
                owned[identity] = TokenCounterRegistration(entry.descriptor, entry.counter)
            object.__setattr__(self, '_registrations', MappingProxyType(owned))
        except (ValueError, TypeError, AttributeError):
            raise TurnAdmissionError('invalidPolicy') from None

    def __setattr__(self, name, value):
        raise AttributeError('immutable registry')

    def resolve(self, identity):
        try:
            return self._registrations[identity]
        except (KeyError, TypeError):
            raise TurnAdmissionError('invalidPolicy') from None


def admit_counter(policy, registry, endpoint):
    if type(policy) is not ContextPolicy or type(registry) is not TokenCounterRegistry:
        raise TurnAdmissionError('invalidPolicy')
    registration = registry.resolve(policy.counter_identity)
    descriptor = registration.descriptor
    if (descriptor.provider, descriptor.model) != (endpoint.provider, endpoint.model):
        raise TurnAdmissionError('unsupportedCapability')
    if policy.context_window_tokens > descriptor.context_capacity_tokens:
        raise TurnAdmissionError('invalidPolicy')
    return registration


def count_owned(registration, native_request):
    try:
        request = json.loads(encode_owned(native_request))
        count = registration.counter.count_tokens(request)
        if type(count) is not int or not 0 <= count <= MAX_INTEGER:
            raise ValueError('count bound')
        return count
    except Exception:
        raise TurnAdmissionError('invalidPolicy') from None
