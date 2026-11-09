"""strict_json: the answer is one strict JSON document.

Duplicate keys and NaN/Infinity are rejected, as Runtime's own evidence parser
rejects them. Surrounding whitespace is allowed; code fences and prose are
not. There is no repair: bind it with retry or fail.
"""

from ..accounting_capture import strict_json as _parse
from ..rules import Guard, Verdict


def _check(stage, context):
    try:
        _parse(context.draft)
    except Exception:  # Any parser rejection, including excessive nesting, is a violation.
        return Verdict(False, reason='answer is not one strict JSON document')
    return Verdict(True)


def strict_json(*, input_access='shared'):
    return Guard('strict_json', 'strict-json-v1', ('before_answer', 'final'), _check, input_access=input_access)
