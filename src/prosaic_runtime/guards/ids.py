"""args_reference_seen_ids: tool arguments may only reference ids already seen this turn.

Settings declare the argument keys that hold ids (``keys=["recordId"]``),
searched at any depth, and optionally a ``pattern`` (a glob such as
``REC-*``) that marks id-shaped string values under any key. "Seen" means
present in this turn's tool results, the user message, or host-supplied input
data. Conversation history never counts. Whole-number floats read as their
integer text on both sides (4242.0 is 4242); a bool or a non-whole float under
an id key is refused. No repair: bind it with retry (the model gets an error
result instead of the call) or fail.
"""

from fnmatch import fnmatchcase
import json
import math
import re

from ..rules import Guard, Verdict

_MAX_VALUE = 1024
_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['keys'],
    'properties': {
        'keys': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 64,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        },
        'pattern': {'type': 'string', 'minLength': 1, 'maxLength': 128},
    },
}


def _leaves(value):
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
        else:
            yield node


def _id_text(value):
    """Canonical id text, or None when the value cannot be an id.

    Whole-number floats read as their integer text on both sides, so 4242.0
    (which JSON Schema accepts as "integer") is checked as 4242.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if math.isfinite(value) and value.is_integer() else None
    return value if isinstance(value, str) else None


def _seen(value):
    return {text for leaf in _leaves(value) if (text := _id_text(leaf)) is not None}


def _referenced(arguments, keys, pattern):
    """(id texts, invalid values) referenced by the arguments."""
    found, invalid, stack = [], [], [arguments]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if key in keys:
                    for leaf in _leaves(value):
                        text = _id_text(leaf)
                        if text is not None:
                            found.append(text)
                        elif leaf is not None:  # A bool or non-whole float is never an id.
                            invalid.append(json.dumps(leaf))
                elif (
                    pattern is not None
                    and isinstance(value, str)
                    and len(value) <= _MAX_VALUE
                    and (fnmatchcase(value, pattern))
                ):
                    found.append(value)
                if isinstance(value, (dict, list)) and key not in keys:
                    stack.append(value)
        elif isinstance(node, list):
            for item in node:
                if (
                    pattern is not None
                    and isinstance(item, str)
                    and len(item) <= _MAX_VALUE
                    and (fnmatchcase(item, pattern))
                ):
                    found.append(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
    return list(dict.fromkeys(found)), list(dict.fromkeys(invalid))


def _in_message(value, message):
    return re.search(r'(?<![A-Za-z0-9_-])' + re.escape(value) + r'(?![A-Za-z0-9_-])', message) is not None


def _check(stage, context):
    keys = frozenset(context.settings['keys'])
    referenced, invalid = _referenced(context.arguments, keys, context.settings.get('pattern'))
    if not referenced and not invalid:
        return Verdict(True)
    seen = _seen(list(context.tool_results)) | _seen(context.input_data)
    unseen = [v for v in referenced if v not in seen and not _in_message(v, context.user_message)]
    refused = tuple(invalid + unseen)
    if not refused:
        return Verdict(True)
    return Verdict(False, reason='tool arguments reference ids not seen this turn', refused_values=refused[:64])


def args_reference_seen_ids():
    return Guard(
        'args_reference_seen_ids', 'args-reference-seen-ids-v1', ('before_tool',), _check, settings_schema=_SCHEMA
    )
