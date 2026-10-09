"""Anthropic token evidence, with cache-write costs explicitly unsupported."""
from .accounting import _quantity, normalize_usage

TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def _zero_details(value):
    if value is None:
        return True
    if type(value) is dict:
        return all(_zero_details(v) for v in value.values())
    return type(value) is int and value == 0


def normalize_anthropic_usage(raw):
    result = normalize_usage(None)
    if raw is None:
        return result
    if type(raw) is not dict:
        return {**result, 'status': 'untrusted'}
    for key in TOKEN_FIELDS:
        if key in raw and not _quantity(raw[key]):
            return {**result, 'status': 'untrusted'}
    result['output_tokens'] = raw.get('output_tokens')
    result['cached_input_tokens'] = raw.get('cache_read_input_tokens')
    if 'input_tokens' in raw:
        inp = sum(raw.get(k, 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
        if not _quantity(inp):
            return {**result, 'status': 'untrusted'}
        result['input_tokens'] = inp
    if result['input_tokens'] is not None and result['output_tokens'] is not None:
        total = result['input_tokens'] + result['output_tokens']
        if not _quantity(total):
            return {**result, 'status': 'untrusted'}
        result.update(total_tokens=total, status='reported')
    # Cache-write pricing cannot be represented by the existing rate-card contract.
    unsupported = bool(raw.get('cache_creation_input_tokens', 0))
    if raw.get('service_tier') not in (None, 'standard') or raw.get('inference_geo') not in (None, 'global'):
        unsupported = True
    for key, value in raw.items():
        if key not in TOKEN_FIELDS and key not in {'service_tier', 'inference_geo'} and not _zero_details(value):
            unsupported = True
    if unsupported:
        result['status'] = 'unsupported'
    return result


class UsageSnapshots:
    """Merge cumulative event snapshots; decreases invalidate accounting evidence."""
    def __init__(self):
        self.raw = None
        self.conflict = False

    def update(self, value):
        if value is None:
            return
        if type(value) is not dict:
            self.conflict = True
            return
        previous = self.raw or {}
        if _snapshot_conflict(previous, value):
            self.conflict = True
        for key in TOKEN_FIELDS:
            if key in value:
                new, old = value[key], previous.get(key)
                if not _quantity(new) or (old is not None and (not _quantity(old) or new < old)):
                    self.conflict = True
        self.raw = _merge_snapshots(previous, value)

    def normalized(self):
        usage = normalize_anthropic_usage(self.raw)
        return {**usage, 'status': 'untrusted'} if self.conflict else usage


def _snapshot_conflict(previous, current):
    for key in previous.keys() & current.keys():
        old, new = previous[key], current[key]
        if type(old) is dict and type(new) is dict:
            if _snapshot_conflict(old, new):
                return True
        elif _quantity(old) and _quantity(new):
            if new < old:
                return True
        elif old != new:
            return True
    return False


def _merge_snapshots(previous, current):
    merged = dict(previous)
    for key, value in current.items():
        if type(value) is dict and type(previous.get(key)) is dict:
            merged[key] = _merge_snapshots(previous[key], value)
        else:
            merged[key] = value
    return merged
