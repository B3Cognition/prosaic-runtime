"""value_in_tool_output: stated figures must come from this turn's tool results.

In the answer, only currency-marked amounts (``EUR 1.4M``, ``€12``) and
multiples written with x (``4.7x``) are checked. Bare numbers are not:
a documented false negative.

Source figures, from this turn's tool results only: numeric fields; text
fields that are wholly a formatted number; and, inside longer text, only
currency- or x-marked amounts. Keys whose last word is id, ids, reference,
date or year are never read, in any case style (``id``, ``homes_id``,
``playerId``, ``recordIDs``, ``startDate``, ``fiscal_year``). A stated figure matches when a
source rounds or truncates to it at the figure's written precision (sign is
ignored). When the data names a currency (a ``currency``/``currencyCode`` key
on the object or an ancestor, or the marker of a text amount), a different
stated currency is unmatched. Markers default to a vendored common subset of
ISO 4217 codes (``DEFAULT_CODES``) plus the symbols € £ $ (read as EUR, GBP,
USD), written before the number; the ``currencies`` setting extends them. So
``ISO 9001`` or ``TOP 5`` is never read as an amount. The subset deliberately
omits codes that collide with ordinary words (PHP, ALL, TOP); add them through
``currencies`` when needed. No repair.
"""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
import math
import re

from ..rules import Guard, Verdict

SYMBOLS = {'€': 'EUR', '£': 'GBP', '$': 'USD'}
# A common subset of ISO 4217 alphabetic codes, not the full list.
DEFAULT_CODES = (
    'AED', 'AUD', 'BRL', 'CAD', 'CHF', 'CLP', 'CNY', 'COP', 'CZK', 'DKK', 'EGP', 'EUR', 'GBP', 'HKD',
    'HUF', 'IDR', 'ILS', 'INR', 'ISK', 'JPY', 'KES', 'KRW', 'MAD', 'MXN', 'MYR', 'NGN', 'NOK', 'NZD',
    'PEN', 'PKR', 'PLN', 'QAR', 'RON', 'RUB', 'SAR', 'SEK', 'SGD', 'THB', 'TRY', 'TWD', 'UAH', 'USD',
    'VND', 'ZAR',
)  # fmt: skip
_SCALES = {
    None: Decimal(1),
    'K': Decimal(10) ** 3,
    'k': Decimal(10) ** 3,
    'M': Decimal(10) ** 6,
    'B': Decimal(10) ** 9,
    'bn': Decimal(10) ** 9,
}
_NUMBER = r'(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)'
_END = r'(?![A-Za-z0-9]|[.,]\d)'
_MULTIPLE = re.compile(r'(?<![\w.])' + _NUMBER + r'[x×]' + _END)
_WHOLE = re.compile(r'\s*' + _NUMBER + r'\s*')
_WORDS = re.compile(r'[A-Z]{2,}s?(?![a-z])|[A-Z]?[a-z]+|[A-Z]|\d+')  # Acronyms may take a plural s.
_EXCLUDED_WORDS = frozenset({'id', 'ids', 'reference', 'date', 'year'})


def _excluded(key):
    """Never read id/reference/date/year fields, in snake, camel or kebab case."""
    words = _WORDS.findall(key)
    return bool(words) and words[-1].lower() in _EXCLUDED_WORDS


_CURRENCY_KEY = re.compile(r'(?i)currency(?:_?code)?')
_ISO = re.compile(r'[A-Z]{3}')
_MAX_REPORTED = 64
_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'currencies': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 64,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 8},
        }
    },
}


def _amounts(extra):
    markers = {*DEFAULT_CODES, *SYMBOLS, *(extra or ())}
    marker = '|'.join(re.escape(m) for m in sorted(markers, key=lambda m: (-len(m), m)))
    return re.compile(r'(?<![A-Za-z0-9])(?P<cur>' + marker + r')[  ]?' + _NUMBER + r'(?P<scale>bn|[KkMB])?' + _END)


def _code(marker):
    return SYMBOLS.get(marker, marker.upper())


def _decimal(text):
    return Decimal(text.replace(',', ''))


def _sources(results, amounts):
    """(value, currency or None, kind) for every readable source figure."""
    found = []

    def text(value, currency):
        whole = _WHOLE.fullmatch(value)
        if whole:
            found.append((_decimal(whole['num']), currency, 'number'))
            return
        for match in amounts.finditer(value):
            found.append((_decimal(match['num']) * _SCALES[match['scale']], _code(match['cur']), 'currency'))
        for match in _MULTIPLE.finditer(value):
            found.append((_decimal(match['num']), None, 'x'))

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


def _matches(stated, decimals, scale, source):
    try:
        scaled = abs(source) / scale
        step = Decimal(1).scaleb(-decimals)
        return stated in (scaled.quantize(step, ROUND_HALF_UP), scaled.quantize(step, ROUND_DOWN))
    except InvalidOperation:
        return False


def _check(stage, context):
    markers = context.settings.get('currencies')
    amounts = _amounts(markers)
    sources = _sources(context.tool_results, amounts)
    stated = [
        (m.start(), m.group(0), _code(m['cur']), m['num'], _SCALES[m['scale']]) for m in amounts.finditer(context.draft)
    ]
    stated += [(m.start(), m.group(0), None, m['num'], Decimal(1)) for m in _MULTIPLE.finditer(context.draft)]
    refused = []
    for _, written, currency, number, scale in sorted(stated):
        decimals = len(number.split('.', 1)[1]) if '.' in number else 0
        value = _decimal(number)
        if currency is None:
            pool = [s for s, _, kind in sources if kind in ('number', 'x')]
        else:
            pool = [
                s
                for s, source_currency, kind in sources
                if kind in ('number', 'currency') and source_currency in (None, currency)
            ]
        if not any(_matches(value, decimals, scale, source) for source in pool):
            refused.append(written)
    if not refused:
        return Verdict(True)
    return Verdict(
        False,
        reason="stated figures not found in this turn's tool results",
        refused_values=tuple(refused[:_MAX_REPORTED]),
    )


def value_in_tool_output():
    # Refused values are regex-constrained figures ("EUR 650", "3x"), which cannot carry
    # instructions, so this guard opts in to echoing them in answer-stage retry feedback.
    return Guard(
        'value_in_tool_output',
        'value-in-tool-output-v1',
        ('before_answer', 'final'),
        _check,
        echo_values=True,
        settings_schema=_SCHEMA,
    )
