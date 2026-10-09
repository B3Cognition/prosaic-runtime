"""Host response guards shared by native JSON and stream readers."""


class LimitExceeded(ValueError):
    pass


def set_response_timeout(response, remaining):
    """Find urllib's socket through bounded/accounting and HTTPError wrappers."""
    pending, seen = [response], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        sock = getattr(current, '_sock', None)
        if sock is not None:
            sock.settimeout(max(0.001, remaining))
            return
        pending.extend(getattr(current, name, None) for name in ('response', 'fp', 'raw'))
