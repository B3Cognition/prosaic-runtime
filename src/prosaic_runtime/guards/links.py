"""no_wrapped_links: a complete Markdown link must not sit inside emphasis or code markers.

Renderers that support only a Markdown subset show ``**[text](target)**`` or
`` `[text](target)` `` as raw text. The repair removes one layer of wrapping
markers per pass and keeps the link; nesting such as ``***[a](/b)***`` is
unwrapped across the bounded fix passes. Bare URLs, <autolinks> and
reference-style links are out of scope, as is the target's form.
"""

import re

from ..rules import Guard, Verdict

_LINK = r'\[[^\[\]\n]*\]\([^()\s]*\)'
_WRAPPED = re.compile(r'(\*\*|__|\*|_|`)(' + _LINK + r')\1')
_MAX_REPORTED = 64


def _check(stage, context):
    text = context.draft
    found = [match.group(0) for match in _WRAPPED.finditer(text)]
    if not found:
        return Verdict(True)
    return Verdict(
        False,
        reason='complete link wrapped in emphasis or code markers',
        refused_values=tuple(found[:_MAX_REPORTED]),
        fixed=_WRAPPED.sub(r'\2', text),
    )


def no_wrapped_links():
    return Guard('no_wrapped_links', 'no-wrapped-links-v1', ('before_answer', 'final'), _check, fixes=True)
