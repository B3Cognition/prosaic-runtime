"""no_wrapped_links: a complete Markdown link must not sit inside emphasis or code markers.

Renderers that support only a Markdown subset show ``**[text](target)**`` or
`` `[text](target)` `` as raw text. The repair removes one layer of wrapping
markers per pass and keeps the link; nesting such as ``***[a](/b)***`` is
unwrapped across the bounded fix passes. A link may carry a title
(``[a](/b "Title")``) and its target may contain balanced parentheses
(``[a](/wiki/Shelf_(north))``) or be written ``<...>``.

Code is never rewritten: text inside fenced code blocks and inside inline code
spans is left alone, except a single-backtick span that is exactly one link,
which is the wrapped form this guard repairs. Bare URLs, <autolinks>,
reference-style links and the target's form are out of scope.
"""

import re

from ..rules import Guard, Verdict

_DESTINATION = r'(?:<[^<>\n]*>|(?:[^\s()]|\([^\s()]*\))*)'
_TITLE = r"""(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?"""
_LINK = r'\[[^\[\]\n]*\]\([ \t]*' + _DESTINATION + _TITLE + r'[ \t]*\)'
_WRAPPED = re.compile(r'(\*\*|__|\*|_|`)(' + _LINK + r')\1')
_BARE_LINK = re.compile(_LINK)
_FENCE = re.compile(r'(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)')
_CODE_SPAN = re.compile(r'(?s)(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)')
_MAX_REPORTED = 64


def _protected(text):
    """Character ranges that are code: fenced blocks and inline spans other than a lone wrapped link."""
    ranges = [match.span() for match in _FENCE.finditer(text)]
    # Blank out fenced blocks first, so a backtick inside a fence can never pair with one outside it.
    masked = list(text)
    for start, end in ranges:
        masked[start:end] = ' ' * (end - start)
    for span in _CODE_SPAN.finditer(''.join(masked)):
        if len(span.group(1)) == 1 and _BARE_LINK.fullmatch(span.group(2)):
            continue  # `[a](/b)`: exactly the wrapped link this guard repairs.
        ranges.append(span.span())
    return ranges


def _wrapped(text):
    ranges = _protected(text)
    return [
        match
        for match in _WRAPPED.finditer(text)
        if not any(match.start() < end and start < match.end() for start, end in ranges)
    ]


def _check(stage, context):
    text = context.draft
    found = _wrapped(text)
    if not found:
        return Verdict(True)
    pieces, last = [], 0
    for match in found:
        pieces += [text[last : match.start()], match.group(2)]
        last = match.end()
    return Verdict(
        False,
        reason='complete link wrapped in emphasis or code markers',
        refused_values=tuple(match.group(0) for match in found[:_MAX_REPORTED]),
        fixed=''.join(pieces) + text[last:],
    )


def no_wrapped_links(*, input_access='shared'):
    return Guard('no_wrapped_links', 'no-wrapped-links-v1', ('before_answer', 'final'), _check, fixes=True, input_access=input_access)
