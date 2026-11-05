"""args_reference_seen_ids: tool arguments may only reference ids already seen this turn.

Settings declare the argument keys that hold ids (``keys=["recordId"]``),
searched at any depth, and optionally a ``pattern`` (a glob such as
``REC-*``) that marks id-shaped values under any key: a whole value without
whitespace, a word inside longer text (``"id REC-999"``, ``"close INV.2024.77"``)
or an object key (``{"REC-9": {...}}``). Every referenced
value is checked whatever its length. An empty or whitespace-only string is
never an id and is refused, as is a bool or a non-whole float under an id key.
Whole-number floats read as their integer text (4242.0 is 4242).

"Seen" this turn, from tool results (as guards see them) and host input data:

- a text id equal to any value;
- a numeric id equal to a value under an id key: one named in ``keys``, or
  whose last word is id, ids, ref, refs, reference or references. A count,
  page number or price is never evidence of a numeric id.

And as a whole word (delimited by anything other than a letter, digit, ``_``,
``-`` or combining mark, in any script, after NFC normalization) in the user
message or in file lines read by built-in tools (``ToolResults.builtin``); a
numeric id counts in the user message only after an id cue, such as ``#12``,
``id 12`` or a word of an id key (``record 12`` for ``recordId``). Object keys
of results or input data, custom result prose and conversation history never
count as evidence. No repair: bind it
with retry (the model gets an error result instead of the call) or fail.
"""

from fnmatch import fnmatchcase
import json
import math
import re
import unicodedata

from ..rules import Guard, Verdict

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
# Word characters: letters, digits, _ and - in any script, plus combining marks, so
# "cafe" is never a word of a decomposed "café".
_MARKS = '̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯'
_WORD = re.compile(r'[\w' + _MARKS + '-]+')
_WORD_CHAR = re.compile(r'[\w' + _MARKS + '-]')
_NUMERIC = re.compile(r'-?\d+')
_KEY_WORDS = re.compile(r'[A-Z]{2,}s?(?![a-z])|[A-Z]?[a-z]+|[A-Z]|\d+')  # Acronyms may take a plural s.
_ID_WORDS = frozenset({'id', 'ids', 'ref', 'refs', 'reference', 'references'})
_FILE_TEXT_KEYS = frozenset({'lines', 'text'})  # Built-in reader facts (see rule_facts).
_EDGE_PUNCTUATION = '\'"()[]{}<>,;:!?.*_`'
_MAX_OCCURRENCES = 100_000  # Per check; a search that would need more fails closed.


def _nfc(text):
    return unicodedata.normalize('NFC', text)


def _id_text(value):
    """Canonical id text, or None when the value cannot be an id."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if math.isfinite(value) and value.is_integer() else None
    return value if isinstance(value, str) else None


def _leaves(value, key=None):
    """(leaf, nearest object key) pairs; list items inherit their list's key."""
    stack = [(value, key)]
    while stack:
        node, owner = stack.pop()
        if isinstance(node, dict):
            stack.extend((child, name) for name, child in reversed(list(node.items())))
        elif isinstance(node, list):
            stack.extend((child, owner) for child in reversed(node))
        else:
            yield node, owner


def _id_key(key, keys):
    if type(key) is not str:
        return False
    words = _KEY_WORDS.findall(key)
    return key in keys or (bool(words) and words[-1].lower() in _ID_WORDS)


def _pattern_ids(value, pattern):
    """Pattern ids in a string: the whole value without whitespace, or words inside it.

    Words are both runs of word characters and whitespace-separated tokens with
    surrounding punctuation removed, so ``INV.2024.77`` or ``ORD/12`` inside a
    sentence is found too.
    """
    if pattern is None or not isinstance(value, str):
        return []
    candidates = [token.strip(_EDGE_PUNCTUATION) for token in value.split()] + _WORD.findall(value)
    if not any(character.isspace() for character in value):
        candidates.insert(0, value)
    return [candidate for candidate in dict.fromkeys(candidates) if candidate and fnmatchcase(candidate, pattern)]


def _referenced(arguments, keys, pattern):
    """(id texts, invalid values) referenced by the arguments, including object keys that match the pattern."""
    found, invalid, stack = [], [], [arguments]
    while stack:
        node = stack.pop(0)
        children = node.items() if isinstance(node, dict) else ((None, item) for item in node)
        for key, value in children:
            if key is not None and key in keys:
                for leaf, _ in _leaves(value):
                    text = _id_text(leaf)
                    if text is not None:
                        found.append(text)
                    elif leaf is not None:  # A bool or non-whole float is never an id.
                        invalid.append(json.dumps(leaf))
                continue
            if key is not None:
                found += _pattern_ids(key, pattern)
            if isinstance(value, (dict, list)):
                stack.append(value)
            else:
                found += _pattern_ids(value, pattern)
    # An empty or blank string names nothing, so it can never be shown to have been seen.
    invalid += [json.dumps(text) for text in found if not text.strip()]
    found = [text for text in found if text.strip()]
    return list(dict.fromkeys(found)), list(dict.fromkeys(invalid))


def _cue(keys):
    words = {'id'} | {word.lower() for key in keys for word in _KEY_WORDS.findall(key) if not word.isdigit()}
    words |= {key.lower() for key in keys}
    alternatives = '|'.join(sorted((re.escape(word) for word in words), key=len, reverse=True))
    return re.compile(r'(?i)(?:#|\b(?:' + alternatives + r')s?(?:\s{1,3}|\s{0,3}[:=#]\s{0,3}))(-?\d+)(?![\w-])')


class _Evidence:
    """Everything that can show an id was seen this turn, indexed once per check."""

    def __init__(self, context, keys):
        results = list(context.tool_results)
        builtin = getattr(context.tool_results, 'builtin', ())
        self.texts, self.numbers = set(), set()
        for leaf, key in _leaves([results, context.input_data]):
            text = _id_text(leaf)
            if text is None:
                continue
            if _NUMERIC.fullmatch(text):
                if _id_key(key, keys):
                    self.numbers.add(text)
            else:
                self.texts.add(_nfc(text))
        # Whole words count only in the user message and in file lines read by built-in tools,
        # never in the prose of a custom result.
        files = [
            leaf
            for result, from_builtin in zip(results, builtin)
            if from_builtin
            for leaf, key in _leaves(result)
            if isinstance(leaf, str) and key in _FILE_TEXT_KEYS
        ]
        message = context.user_message or ''
        self.prose = [_nfc(text) for text in [message, *files]]
        self.words = {word for text in self.prose for word in _WORD.findall(text)}
        self.cued = {match.group(1) for match in _cue(keys).finditer(_nfc(message))}
        self.budget = _MAX_OCCURRENCES

    def _delimited(self, value, text):
        start = 0
        while (index := text.find(value, start)) != -1:
            self.budget -= 1
            if self.budget < 0:
                return False
            end = index + len(value)
            before = text[index - 1] if index else ''
            after = text[end] if end < len(text) else ''
            if not (before and _WORD_CHAR.fullmatch(before)) and not (after and _WORD_CHAR.fullmatch(after)):
                return True
            start = index + 1
        return False

    def seen(self, value):
        value = _nfc(value)
        if _NUMERIC.fullmatch(value):
            return value in self.numbers or value in self.cued
        if value in self.texts:
            return True
        if not any(character.isalnum() for character in value):
            return False  # Punctuation is never a word of text.
        if _WORD.fullmatch(value):
            return value in self.words
        return any(self._delimited(value, text) for text in self.prose)


def _check(stage, context):
    keys = frozenset(context.settings['keys'])
    referenced, invalid = _referenced(context.arguments, keys, context.settings.get('pattern'))
    if not referenced and not invalid:
        return Verdict(True)
    evidence = _Evidence(context, keys)
    unseen = [value for value in referenced if not evidence.seen(value)]
    refused = tuple(invalid + unseen)
    if not refused:
        return Verdict(True)
    return Verdict(False, reason='tool arguments reference ids not seen this turn', refused_values=refused[:64])


def args_reference_seen_ids():
    return Guard(
        'args_reference_seen_ids', 'args-reference-seen-ids-v1', ('before_tool',), _check, settings_schema=_SCHEMA
    )
