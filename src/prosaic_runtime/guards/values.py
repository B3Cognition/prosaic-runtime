"""value_in_tool_output: stated figures must come from this turn's tool results.

In the answer, currency-marked amounts and multiples written with x
(``4.7x``) are checked. Bare numbers are not: a documented false negative.

A marker is an ISO code from ``DEFAULT_CODES`` (upper case, or lower or title
case except codes that are ordinary words: TRY, MAD, PEN, COP, RON, CAD, RUB),
a code added through the ``currencies`` setting (as written), a symbol
(€ £ $, read as EUR, GBP, USD) or a prefixed dollar (US$, A$, C$, HK$, ...).
Codes and prefixed dollars come before or after the number; symbols also do
(``€12``, ``12 €``, ``12 EUR``). Suffix euro/euros denotes EUR; cent/cents
denotes hundredths of an unspecified currency. These words are case-insensitive
whole words, never prefixes of ordinary words. Between marker and number up to 32 characters of
spacing, Markdown emphasis, code ticks or ``~``/``≈`` may stand, crossing a line
break only straight into the digits. A sign (ASCII plus or minus, Unicode
minus, dashes, hyphens) may stand before the number, or before the marker when
it touches the marker and follows no word character, so a Markdown bullet
(``- $5m``) or a range (``$10-$20``) is never a minus. Accounting parentheses
after the marker (``$(12)``) are negative.

A figure's digits may group thousands with ``,`` ``.`` ``'`` ``’`` or spaces
and use ``.`` or ``,`` for decimals; ``1,500`` is read both ways and ``$.75``
is a decimal. Scales (see ``_SCALES``) are a letter touching the digits
(``€5m``), an upper-case letter after one space (``€5 M``), or a word after
spacing, emphasis or a hyphen (``€1.4 million``, ``$5-million``, ``INR 5
crore``). A marked figure this grammar cannot read (``EUR 1.2.3``,
``EUR 12abc``, ``$5_000``) or a range (``$3-5m``, ``3-5m €``) is refused, never
skipped or read as a prefix. A sign touching an x-multiple (``-4.7x``) is part
of it; a bullet (``- 4.7x``) is not.

Overlapping markers never hide a different number. Conflicting units on one
number or unsupported compounds (``120 cents USD``) are refused; the same text
gives no source evidence. Separate complete prefixed amounts remain separate.

Sources, from this turn's tool results only: numeric fields; text fields that
are wholly a figure; and marked figures or x-multiples inside longer text, read
by the same scan and boundary checks as the answer, so a figure the answer
could not state gives no evidence (``EUR 12abc`` never supports ``EUR 12``). Keys
whose last word is id, ids, reference, date, year, path, paths, url or urls,
and ``sha256``, are never read. A stated figure matches a source that rounds to
it (half up or half even) at the figure's written precision and scale; the sign
must agree unless the ``sign`` setting is ``magnitude``. When the data names a
currency (a ``currency``/``currencyCode`` key on the object or an ancestor, or
a text figure's marker), a different stated currency is unmatched. No repair.
"""

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
import math
import re

from ..rules import Guard, Verdict

SYMBOLS = {'€': 'EUR', '£': 'GBP', '$': 'USD'}
PREFIXED = {
    'US$': 'USD', 'A$': 'AUD', 'AU$': 'AUD', 'C$': 'CAD', 'CA$': 'CAD', 'HK$': 'HKD', 'NZ$': 'NZD',
    'S$': 'SGD', 'R$': 'BRL', 'MX$': 'MXN',
}  # fmt: skip
# A common subset of ISO 4217 alphabetic codes, not the full list.
DEFAULT_CODES = (
    'AED', 'AUD', 'BRL', 'CAD', 'CHF', 'CLP', 'CNY', 'COP', 'CZK', 'DKK', 'EGP', 'EUR', 'GBP', 'HKD',
    'HUF', 'IDR', 'ILS', 'INR', 'ISK', 'JPY', 'KES', 'KRW', 'MAD', 'MXN', 'MYR', 'NGN', 'NOK', 'NZD',
    'PEN', 'PKR', 'PLN', 'QAR', 'RON', 'RUB', 'SAR', 'SEK', 'SGD', 'THB', 'TRY', 'TWD', 'UAH', 'USD',
    'VND', 'ZAR',
)  # fmt: skip
WORD_CODES = frozenset({'TRY', 'MAD', 'PEN', 'COP', 'RON', 'CAD', 'RUB'})  # Upper case only.
_SCALES = {'k': 3, 'thousand': 3, 'hundred': 2, 'lakh': 5, 'crore': 7, 'm': 6, 'mm': 6, 'mn': 6, 'mln': 6}
_SCALES.update({'mio': 6, 'million': 6, 'millionen': 6, 'b': 9, 'bn': 9, 'bln': 9, 'mrd': 9, 'billion': 9})
_SCALES.update({'milliard': 9, 'milliarde': 9, 'milliarden': 9, 't': 12, 'tn': 12, 'trillion': 12})
_SIGN = '[-+−–—‑‐]'
_SEP = "[.,'’    ]"
_SPACE = '[ \t    ]'
_GAP = r'[ \t    *_~≈`]'  # Spacing, emphasis, code ticks, "about"; never a newline.
_BODY = r'(?P<open>\()?(?P<num>\d(?:' + _SEP + r'?\d){0,40}|\.\d{1,40})'
_WORD_SCALES = (
    'thousand|hundred|lakhs?|crores?|millionen|millions?|milliarden?|milliard|billions?|trillions?'
    r'|mio\.?|mrd\.?|mln|bln|mn|mm|bn|tn'
)
_SCALE = (
    r'(?:(?:[*_`]{0,3}' + _SPACE + '{0,3}|-)(?P<word>(?i:' + _WORD_SCALES + r'))(?![A-Za-z])'
    r'|(?P<letter>[kKmMbBT])(?![A-Za-z])|' + _SPACE + r'(?P<upper>[KMBT])(?![A-Za-z]))?'
    r'(?P<close>(?(open)\)?))'
)
_FIGURE = r'(?:(?P<sign>' + _SIGN + r')' + _GAP + r'{0,3})?' + _BODY + _SCALE
# After a figure: a word character, or a range (EUR 3-5m, $3 to 5m), means the figure cannot be read.
_UNREADABLE_AFTER = re.compile(r'[\w]|' + _SPACE + r'?[-–—]' + _SPACE + r'?[\d.]|' + _SPACE + r'+to' + _SPACE + r'+\d')
# Before a figure's digits: the end of a range (3-5m €, 3 - 5 €, 3 to 5 €) means it cannot be read.
_RANGE_BEFORE = re.compile(
    r'\d' + _SPACE + r'{0,2}[-−–—‑‐]' + _GAP + r'{0,3}\(?$|\d' + _SPACE + r'+to' + _SPACE + r'+\(?$'
)
_RANGE_REACH = 16
_WHOLE = re.compile(r'\s*' + _FIGURE + r'\s*')
# A sign touching a multiple (-4.7x, -**4.7x**) is part of it; a bullet (- 4.7x) is not.
_MULTIPLE = re.compile(
    r'(?:(?<![\w.])(?P<sign>' + _SIGN + r')[*_`]{0,3})?(?<![\w.])(?P<num>\d+(?:\.\d+)?)[x×](?![A-Za-z0-9]|[.,]\d)'
)
_WORDS = re.compile(r'[A-Z]{2,}s?(?![a-z])|[A-Z]?[a-z]+|[A-Z]|\d+')  # Acronyms may take a plural s.
_EXCLUDED_WORDS = frozenset({'id', 'ids', 'reference', 'date', 'year', 'path', 'paths', 'url', 'urls'})
_CURRENCY_KEY = re.compile(r'(?i)currency(?:_?code)?')
_ISO = re.compile(r'[A-Z]{3}')
_MAX_REPORTED = 64
_MAX_WRITTEN = 64
_SUFFIX_WORDS = {'euro': 'EUR', 'euros': 'EUR', 'cent': None, 'cents': None}
_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'currencies': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 64,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 8},
        },
        'sign': {'type': 'string', 'enum': ['strict', 'magnitude']},
    },
}


def _excluded(key):
    """Never read id/reference/date/year/path/url fields, in snake, camel or kebab case."""
    words = _WORDS.findall(key)
    return key.lower() == 'sha256' or (bool(words) and words[-1].lower() in _EXCLUDED_WORDS)


def _markers(extra):
    """Marker spelling -> currency code."""
    markers = {}
    for code in DEFAULT_CODES:
        spellings = {code} if code in WORD_CODES else {code, code.lower(), code.title()}
        markers.update(dict.fromkeys(spellings, code))
    markers.update({marker: marker.upper() for marker in extra or ()})
    return markers | SYMBOLS | PREFIXED


def _alternatives(markers, *, suffix=False):
    """Longest first, with word boundaries on the marker's exposed side."""
    ordered = sorted(markers, key=lambda m: (-len(m), m))
    return '|'.join(
        ('(?<![A-Za-z0-9])' if m[0].isalpha() and not suffix else '')
        + ('(?i:' + re.escape(m) + ')' if suffix and m.lower() in _SUFFIX_WORDS else re.escape(m))
        + ('(?![A-Za-z])' if suffix and m[-1].isalpha() else '')
        for m in ordered
    )


def _patterns(extra):
    """Prefix/suffix scanners, marker identities, and unsupported trailing units.

    A sign before the marker must touch it (``-$5``, ``-**$5**``) and not follow
    a word character, so a Markdown bullet (``- $5m``) or a range (``$10-$20``)
    is never read as a minus. The gap between marker and number may cross a
    line break only straight into the digits, so ``EUR`` ending one line and a
    bullet ``- 5`` starting the next are never one negative figure.
    """
    markers = _markers(extra)
    prefix = r'(?:(?<!\w)(?P<pre>' + _SIGN + r')[*_`]{0,3})?(?P<cur>' + _alternatives(markers) + ')'
    gap = r'(?:[\s*_~≈`]{0,32}(?=\(?[\d.])|' + _GAP + '{0,32})'
    prefixed = re.compile(prefix + gap + _FIGURE)
    markers.update(_SUFFIX_WORDS)
    suffixes = _alternatives(markers, suffix=True)
    suffixed = re.compile(r'(?<![\w.,])' + _FIGURE + _GAP + '{0,32}(?P<cur>' + suffixes + r')')
    following = re.compile(r'[\s*_~≈`]{1,32}(?:' + suffixes + r')(?!' + gap + _FIGURE + ')')
    return prefixed, suffixed, markers, following


def _readings(body):
    """Every (magnitude, decimals) the digits can mean; empty when they cannot be read."""
    parts = re.split('(' + _SEP + ')', body)
    digits, seps = parts[0::2], parts[1::2]
    if not seps:
        return [(Decimal(body), 0)]
    readings = []
    grouped = 1 <= len(digits[0]) <= 3
    if len(set(seps)) == 1 and grouped and all(len(group) == 3 for group in digits[1:]):
        readings.append((Decimal(''.join(digits)), 0))  # Every separator groups thousands.
    *head, last = seps
    if last in '.,' and last not in head and len(set(head)) <= 1:
        if not head or (grouped and all(len(group) == 3 for group in digits[1:-1])):
            readings.append((Decimal(''.join(digits[:-1]) + '.' + digits[-1]), len(digits[-1])))
    return readings


def _figure(match):
    """(readings, scale, negative) of a figure match, or None when it is unreadable."""
    if bool(match['open']) != bool(match['close']):
        return None
    readings = _readings(match['num'])
    if not readings:
        return None
    unit = (match['word'] or match['letter'] or match['upper'] or '').lower().rstrip('.')
    if unit.endswith('s') and unit[:-1] in _SCALES:
        unit = unit[:-1]  # millions, lakhs, crores
    pre = match['pre'] if 'pre' in match.re.groupindex else None
    sign = match['sign']
    if sign == '-' and 'cur' in match.re.groupindex and 'pre' not in match.re.groupindex:
        end = match.end('sign')
        line_start = match.string.rfind('\n', 0, match.start('sign')) + 1
        if match.string[end:end + 1].isspace() and not match.string[line_start:match.start('sign')].strip():
            sign = None  # A Markdown list bullet, not a negative suffixed amount.
    negative = bool(match['open']) or (sign or '+') != '+' or (pre or '+') != '+'
    return readings, Decimal(10) ** _SCALES.get(unit, 0), negative


def _readable(text, match):
    """False when a word character or range follows the figure, or a range ends at its digits."""
    if _UNREADABLE_AFTER.match(text, match.end()) is not None:
        return False
    start = match.start('num')
    return _RANGE_BEFORE.search(text, max(0, start - _RANGE_REACH), start) is None


def _marked(text, patterns):
    """(start, written, currency, figure or None) for every marked figure in text.

    The answer and the sources share this scan, so a figure either side cannot
    read is None on both: refused in the answer, no evidence in a source, and
    never salvaged as a numeric prefix (``EUR 12abc`` is not ``EUR 12``).
    The leftmost complete amount owns its marker. A shared marker cannot turn
    ``1.20 EUR\n30 days`` into only ``EUR 30``. Conflicting units on the same
    number, and unsupported trailing qualifications, give no source evidence.
    """
    prefixed, suffixed, markers, following = patterns
    prefixes = list(prefixed.finditer(text))
    prefix_at_marker = {match.start('cur'): match for match in prefixes}
    candidates = prefixes + list(suffixed.finditer(text))
    selected = []
    for match in sorted(candidates, key=lambda item: item.start()):
        number = match.span('num')
        marker = match['cur']
        currency = _SUFFIX_WORDS[marker.lower()] if marker.lower() in _SUFFIX_WORDS else markers[marker]
        minor = marker.lower() in ('cent', 'cents')
        if selected and match.start() < selected[-1]['end']:
            previous = selected[-1]
            if number == previous['number']:
                next_prefix = prefix_at_marker.get(match.start('cur'))
                if match.re is suffixed and next_prefix is not None and next_prefix.span('num') != number:
                    continue  # EUR 1.20 USD 0.40: USD belongs to the next prefixed amount.
                if (currency, minor) != previous['unit']:
                    previous['figure'] = None
                    previous['end'] = max(previous['end'], match.end())
            continue
        figure = _figure(match) if _readable(text, match) else None
        if following.match(text, match.end()) is not None:
            figure = None  # e.g. 120 cents USD: never silently discard the explicit currency.
        if figure is not None and minor:
            readings, scale, negative = figure
            figure = readings, scale * Decimal('0.01'), negative
        selected.append(dict(start=match.start(), end=match.end(), number=number,
                             currency=currency, unit=(currency, minor), figure=figure))
    for item in selected:
        yield item['start'], text[item['start']:item['end']].strip()[:_MAX_WRITTEN], item['currency'], item['figure']


def _multiples(text):
    """(start, written, figure) for every x-multiple, its touching sign included."""
    for match in _MULTIPLE.finditer(text):
        number = match['num']
        decimals = len(number.split('.', 1)[1]) if '.' in number else 0
        negative = (match['sign'] or '+') != '+'
        yield match.start(), match.group(0), ([(Decimal(number), decimals)], Decimal(1), negative)


def _sources(results, patterns):
    """(signed value, currency or None, kind) for every readable source figure."""
    found = []

    def add(figure, currency, kind):
        if figure is None:
            return  # Unreadable source text gives no evidence.
        readings, scale, negative = figure
        for magnitude, _ in readings:
            found.append(((-magnitude if negative else magnitude) * scale, currency, kind))

    def text(value, currency):
        whole = _WHOLE.fullmatch(value)
        if whole:
            add(_figure(whole), currency, 'number')
            return
        for _, _, marker, figure in _marked(value, patterns):
            add(figure, marker or currency, 'currency')
        for _, _, figure in _multiples(value):
            add(figure, None, 'x')

    stack = [(result, None) for result in reversed(results)]
    while stack:
        node, currency = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if _CURRENCY_KEY.fullmatch(key) and isinstance(value, str) and _ISO.fullmatch(value.strip()):
                    currency = value.strip()
            stack.extend(
                (value, currency)
                for key, value in reversed(node.items())
                if not _excluded(key) and not _CURRENCY_KEY.fullmatch(key)
            )
        elif isinstance(node, list):
            stack.extend((item, currency) for item in reversed(node))
        elif isinstance(node, bool) or node is None:
            continue
        elif isinstance(node, int) or (isinstance(node, float) and math.isfinite(node)):
            found.append((Decimal(str(node)), currency, 'number'))
        elif isinstance(node, str):
            text(node, currency)
    return found


def _matches(value, decimals, scale, source):
    try:
        step = Decimal(1).scaleb(-decimals)
        scaled = source / scale
        return value in (scaled.quantize(step, ROUND_HALF_UP), scaled.quantize(step, ROUND_HALF_EVEN))
    except InvalidOperation:
        return False


def _stated(draft, patterns):
    """(position, written, currency, figure or None) for every marked figure and multiple."""
    stated = [(start, written, currency, figure, 'currency')
              for start, written, currency, figure in _marked(draft, patterns)]
    stated.extend((start, written, None, figure, 'x') for start, written, figure in _multiples(draft))
    return sorted(stated, key=lambda item: item[0])


def _check(stage, context):
    patterns = _patterns(context.settings.get('currencies'))
    magnitude = context.settings.get('sign') == 'magnitude'
    sources = _sources(context.tool_results, patterns)
    refused = []
    for _, written, currency, figure, kind in _stated(context.draft, patterns):
        if figure is None:
            refused.append(written)  # A marked figure in a format this guard cannot read.
            continue
        readings, scale, negative = figure
        kinds = ('number', kind)
        pool = [s for s, c, kind in sources if kind in kinds and (currency is None or c in (None, currency))]
        if magnitude:
            pool, negative = [abs(s) for s in pool], False
        supported = any(
            _matches(-value if negative else value, decimals, scale, source)
            for value, decimals in readings
            for source in pool
        )
        if not supported:
            refused.append(written)
    if not refused:
        return Verdict(True)
    return Verdict(
        False,
        reason="stated figures not found in this turn's tool results",
        refused_values=tuple(refused[:_MAX_REPORTED]),
    )


def value_in_tool_output(*, input_access='shared', tool_result_contract=None):
    if tool_result_contract not in (None, 'v1'):
        raise ValueError('invalid tool result contract')
    # Refused values are regex-constrained figures ("EUR 650", "3x", "EUR 1.2.3"): a marker,
    # sign, spacing or emphasis, digits, separators and a scale only, which cannot carry
    # instructions, so this guard opts in to echoing them in answer-stage retry feedback.
    return Guard(
        'value_in_tool_output',
        'value-in-tool-output-v2' if tool_result_contract is None else 'value-in-tool-output-v3-tool-result-v1',
        ('before_answer', 'final'),
        _check,
        echo_values=True,
        settings_schema=_SCHEMA,
        input_access=input_access,
    )
