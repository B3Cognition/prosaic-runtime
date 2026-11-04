"""Host policy for required guards: minimum actions and host-authoritative pins."""

import json

from .rule_types import ACTIONS, NAME, STAGES, RuleAdmissionError, settings_ok, validate_settings


def required_guard_specs(required):
    """Normalise host ``required_guards`` to {name: {action, settings, stage}}; None means not pinned.

    Each value is an action (``"retry"``) or a mapping ``{action, settings?, stage?}``.
    """
    if required is None:
        return {}
    if not isinstance(required, dict):
        raise ValueError('required_guards must be a mapping')
    specs = {}
    for name, spec in required.items():
        if type(name) is not str or not NAME.fullmatch(name):
            raise ValueError('required_guards names must be guard names')
        if isinstance(spec, str):
            spec = {'action': spec}
        if not isinstance(spec, dict) or not {'action'} <= spec.keys() <= {'action', 'settings', 'stage'}:
            raise ValueError('required_guards values are an action or {action, settings?, stage?}')
        if spec['action'] not in ACTIONS:
            raise ValueError('required guard action must be fix, retry or fail')
        if spec.get('stage') is not None and spec['stage'] not in STAGES:
            raise ValueError('required guard stage must be an accepted stage')
        if spec.get('settings') is not None and not settings_ok(spec['settings']):
            raise ValueError('required guard settings must be tag-shaped values')
        specs[name] = {
            'action': spec['action'],
            'settings': json.loads(json.dumps(spec.get('settings'))),
            'stage': spec.get('stage'),
        }
    return specs


def check_required_pins(specs, registry):
    """Startup validation of pins against registered guards: misconfiguration fails before any turn."""
    for name, spec in specs.items():
        guard = registry.get(name)
        if guard is None:
            raise ValueError(f'required guard {name} is not registered')
        if spec['stage'] is not None and spec['stage'] not in guard.stages:
            raise ValueError(f'required guard {name}: pinned stage is not supported by the guard')
        if spec['settings'] is not None:
            try:
                validate_settings(guard, spec['settings'])
            except RuleAdmissionError:
                raise ValueError(f'required guard {name}: pinned settings do not match the guard schema') from None


def apply_pins(name, entry, spec):
    """Host values are authoritative; prose may restate them, never change them. Returns settings."""
    if spec is None:
        return entry['settings']
    if spec['stage'] is not None and entry['stage'] != spec['stage']:
        raise RuleAdmissionError('invalidPolicy', f'required guard {name}: stage is pinned by host policy')
    if spec['settings'] is None:
        return entry['settings']
    pinned = spec['settings']
    if any(key not in pinned or value != pinned[key] for key, value in entry['settings'].items()):
        raise RuleAdmissionError('invalidPolicy', f'required guard {name}: settings are pinned by host policy')
    return pinned
