"""Generic guards shipped with Runtime. Inert until a host registers them and prose binds them.

    runtime = ProsaicRuntime(config, guards=generic_guards())

None of these is registered by default; registering nothing changes nothing.
"""

from .ids import args_reference_seen_ids
from .links import no_wrapped_links
from .strict import strict_json
from .values import value_in_tool_output

__all__ = ['generic_guards', 'no_wrapped_links', 'value_in_tool_output', 'args_reference_seen_ids', 'strict_json']


def generic_guards():
    """Fresh registrations of the four generic guards, for an explicit host opt-in."""
    return (no_wrapped_links(), value_in_tool_output(), args_reference_seen_ids(), strict_json())
