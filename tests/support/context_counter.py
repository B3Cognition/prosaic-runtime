"""Exact synthetic byte units; never production tokenizer qualification."""
import json
import prosaic_runtime as api


def fixture_context(provider='openai-compatible', *, model='fixture-model',
                    mode='fullHistory', capacity=4096, reserve=128, count=None,
                    checks=128, byte_allowance=33_554_432, timeout_ms=2000):
    class Counter:
        def count_tokens(self, request):
            return count(request) if count is not None else len(
                json.dumps(request, allow_nan=False).encode('utf-8'))
    descriptor = api.TokenCounterDescriptor(
        identity='synthetic-byte-v1', provider=provider, model=model,
        framing_revision={'openai-compatible': 'openai-chat-v1',
                          'anthropic': 'anthropic-messages-v1'}[provider],
        context_capacity_tokens=1_048_576, implementation_version='1',
        tokenizer_assets_digest='0'*64, template_assets_digest='1'*64,
        counting_kind='exact',
        guarantee='Exact byte units in the synthetic fixture grammar only.')
    registry = api.TokenCounterRegistry({descriptor.identity:
        api.TokenCounterRegistration(descriptor, Counter())})
    policy = api.ContextPolicy(mode, capacity, reserve, 0, descriptor.identity,
                              checks, byte_allowance, timeout_ms)
    return policy, registry
