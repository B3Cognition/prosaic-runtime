"""Per-invocation event sinks; no process-global stdout redirection."""
import builtins
from contextlib import contextmanager
from contextvars import ContextVar
from .telemetry import observe, critical_hook_failed

_sink = ContextVar("prosaic_event_sink", default=None)
_cancel = ContextVar("prosaic_cancel", default=None)


class Cancelled(RuntimeError):
    pass


def check_cancelled():
    callback = _cancel.get()
    if callback is not None and callback():
        raise Cancelled("invocation cancelled")


def emit(event: str, **data):
    check_cancelled()
    if event in {'tool_started', 'tool_completed'}:
        observe(event, **data)
    sink = _sink.get()
    if sink is not None:
        _deliver(sink, {"event": event, **data})


def _deliver(sink, event):
    try:
        sink(event)
    except BaseException:
        critical_hook_failed()
        raise


def print(*values, **kwargs):
    """Compatibility console when no sink is bound; structured output otherwise."""
    check_cancelled()
    sink = _sink.get()
    if sink is None:
        builtins.print(*values, **kwargs)
    else:
        _deliver(sink, {"event": "progress" if kwargs.get("file") else "text", "text": " ".join(map(str, values))})


@contextmanager
def event_context(sink, cancelled=None):
    token = _sink.set(sink if sink is not None else lambda event: None)
    cancellation = _cancel.set(cancelled)
    try:
        yield
    finally:
        _sink.reset(token)
        _cancel.reset(cancellation)
