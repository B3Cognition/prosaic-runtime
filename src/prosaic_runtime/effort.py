"""Pure neutral effort admission against an operator-bound model profile."""

POSITIVE_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
EFFORTS = ('none', *POSITIVE_EFFORTS)
BASE_EFFORTS = POSITIVE_EFFORTS[:3]
PROVIDER_EFFORTS = {
    'openai-compatible': ('none', *POSITIVE_EFFORTS[:5]),
    'claude-code': POSITIVE_EFFORTS[:5],
    'codex-cli': POSITIVE_EFFORTS,
    'anthropic': (),
}


class EffortUnavailableError(ValueError):
    """A valid neutral effort is unavailable for the selected provider/model."""


def validate_effort(value):
    if value is not None and (not isinstance(value, str) or value not in EFFORTS):
        raise ValueError('unsupported effort; expected none, low, medium, high, xhigh, max, or ultra')
    return value


def api_default_effort(features):
    """Preserve the API transport's tolerant, case-insensitive default setting."""
    value = features.get('reasoning_effort')
    if isinstance(value, str) and value.strip().lower() in EFFORTS:
        return value.strip().lower()
    return None


def supported_efforts(value, provider):
    """Own immutable capability metadata; None retains the original contract."""
    if value is None:
        return None
    allowed = PROVIDER_EFFORTS[provider]
    if (not isinstance(value, (list, tuple))
            or any(not isinstance(v, str) or v not in allowed for v in value)
            or len(set(value)) != len(value)):
        raise ValueError(f'unsupported supported_efforts declaration for {provider}')
    return tuple(value)


def effective_efforts(provider, model, supported=None):
    declared = supported_efforts(supported, provider)
    if declared is None:
        from .model_efforts import automatic_efforts
        declared = automatic_efforts(provider, model)
    return BASE_EFFORTS if declared is None else declared


def api_effort_capabilities(model, features, supported=None):
    """Retain 0.9.9 operator defaults for unknown custom API model identities."""
    from .model_efforts import API_OFF_MODELS, automatic_efforts
    if supported is None and api_effort_format(model, features) == 'thinking_toggle':
        return PROVIDER_EFFORTS['openai-compatible']
    if supported is None and (api_effort_format(model, features) == 'chat_template_effort'
                              or model in API_OFF_MODELS):
        return ('none', *(automatic_efforts('openai-compatible', model) or BASE_EFFORTS))
    if supported is None and automatic_efforts('openai-compatible', model) is None:
        default = api_default_effort(features)
        if default in PROVIDER_EFFORTS['openai-compatible'] and default not in BASE_EFFORTS:
            return (*BASE_EFFORTS, default)
    return supported


def api_effort_format(model, features):
    """Choose a trusted wire format, never a prompt-provided extension body."""
    from .model_efforts import automatic_effort_format
    value = features.get('reasoning_effort_format', automatic_effort_format(model))
    if not isinstance(value, str) or value not in (
            'reasoning_effort', 'thinking_toggle', 'chat_template_effort'):
        raise ValueError('unsupported reasoning_effort_format')
    return value


def resolve_effort(value, default=None, *, provider, model, supported=None):
    # Preserve the API's existing opt-out control for legacy/custom profiles.
    if provider == 'openai-compatible' and value is None and default == 'none':
        return 'none'
    value = validate_effort(default if value is None else value)
    if value is None:
        return None
    allowed = effective_efforts(provider, model, supported)
    if value not in PROVIDER_EFFORTS[provider] or value not in allowed:
        raise EffortUnavailableError(f'effort {value!r} is not supported by {provider} model {model!r}; '
                                    'automatic capability data is unavailable or excludes it; '
                                    'supported_efforts can override unknown/custom model capabilities')
    return value
