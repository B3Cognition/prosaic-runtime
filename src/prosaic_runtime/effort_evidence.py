"""Bounded observations of attempted effort controls, never provider compliance.

Only the final encoded wire body / launched command may supply ``sent``.
This module deliberately has no general request/command serializer.
"""
from copy import deepcopy
import json
import re

KEY = 'reasoning_effort_v1'
EFFORTS = frozenset({'none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})
ROW_KEYS = frozenset({'index', 'transport', 'backend', 'profile', 'model', 'provider_call_id',
    'requested_effort', 'configured_effort', 'format', 'sent', 'provider_confirmation'})
DOC_KEYS = frozenset({'version', 'attempt_count', 'complete', 'omitted_attempts', 'attempts'})
MAX_ROWS, MAX_BYTES = 64, 32768
_UNSET = object()


def _label(value):
    if type(value) is not str or not value or not value.isprintable():
        return None
    try:
        return value if len(value.encode('utf-8')) <= 256 else None
    except UnicodeError:
        return None


def _effort(value):
    return value if type(value) is str and value in EFFORTS else None


def _base(transport, backend, profile, model, call_id, requested, configured):
    return dict(index=1, transport=transport, backend=backend, profile=_label(profile),
        model=_label(model), provider_call_id=call_id if type(call_id) is str and
        re.fullmatch('[0-9a-f]{32}', call_id) else None,
        requested_effort=_effort(requested), configured_effort=_effort(configured),
        format='unavailable', sent={}, provider_confirmation='not_observed')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def http_attempt(data, *, backend, profile, call_id, requested, configured):
    row = _base('http', backend, profile, None, call_id, requested, configured)
    try:
        payload = json.loads(data, object_pairs_hook=_pairs,
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if type(payload) is not dict:
            return row
        row['model'] = _label(payload.get('model'))
        sent = {}
        if 'reasoning_effort' in payload:
            value = _effort(payload['reasoning_effort'])
            if value is None:
                return row
            sent['reasoning_effort'] = value
        if 'chat_template_kwargs' in payload:
            template = payload['chat_template_kwargs']
            if type(template) is not dict:
                return row
            if 'enable_thinking' in template:
                value = template['enable_thinking']
                if type(value) is not bool:
                    return row
                sent['chat_template_kwargs'] = {'enable_thinking': value}
        row['sent'] = sent
        row['format'] = ('chat_template_effort' if len(sent) == 2 else
            'reasoning_effort' if 'reasoning_effort' in sent else
            'thinking_toggle' if sent else 'omitted')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    return row


def native_attempt(argv, *, backend, profile, model, requested, configured):
    row = _base('process', backend, profile, model, None, requested, configured)
    if type(argv) is not list or any(type(v) is not str for v in argv):
        return row
    selected = []
    try:
        if backend == 'claude-code':
            positions = [i for i, v in enumerate(argv) if v == '--effort']
            if len(positions) > 1:
                return row
            if positions:
                value = argv[positions[0] + 1]
                if _effort(value) is None:
                    return row
                selected = ['--effort', value]
        elif backend == 'codex-cli':
            positions = [i for i, v in enumerate(argv) if v.startswith('model_reasoning_effort=')]
            if len(positions) > 1:
                return row
            if positions:
                i = positions[0]
                if i == 0 or argv[i - 1] != '-c':
                    return row
                value = json.loads(argv[i].split('=', 1)[1])
                if _effort(value) is None:
                    return row
                if argv[i] != 'model_reasoning_effort=' + json.dumps(value):
                    return row
                selected = ['-c', argv[i]]
        else:
            return row
    except (ValueError, IndexError):
        return row
    row['format'] = ('claude_cli' if backend == 'claude-code' else 'codex_cli') if selected else 'omitted'
    row['sent'] = {'argv': selected}
    return row


def _valid_row(row):
    if type(row) is not dict or set(row) != ROW_KEYS:
        return False
    if type(row['index']) is not int or row['index'] < 1:
        return False
    if row['provider_confirmation'] != 'not_observed':
        return False
    for key in ('profile', 'model'):
        if row[key] is not None and _label(row[key]) != row[key]:
            return False
    for key in ('requested_effort', 'configured_effort'):
        if row[key] is not None and _effort(row[key]) != row[key]:
            return False
    sent, fmt = row['sent'], row['format']
    if type(sent) is not dict:
        return False
    if row['transport'] == 'http':
        if row['backend'] not in {'openai-compatible', 'anthropic'}:
            return False
        cid = row['provider_call_id']
        if cid is not None and (type(cid) is not str or re.fullmatch('[0-9a-f]{32}', cid) is None):
            return False
        if fmt in {'omitted', 'unavailable'}:
            return sent == {}
        keys = {'reasoning_effort'} if fmt == 'reasoning_effort' else {'chat_template_kwargs'} if fmt == 'thinking_toggle' else {'reasoning_effort', 'chat_template_kwargs'} if fmt == 'chat_template_effort' else None
        if keys is None or set(sent) != keys:
            return False
        if 'reasoning_effort' in sent and _effort(sent['reasoning_effort']) is None:
            return False
        if 'chat_template_kwargs' in sent:
            v = sent['chat_template_kwargs']
            if type(v) is not dict or set(v) != {'enable_thinking'} or type(v['enable_thinking']) is not bool:
                return False
        return True
    if row['transport'] == 'process':
        if row['backend'] not in {'claude-code', 'codex-cli'} or row['provider_call_id'] is not None:
            return False
        if fmt == 'unavailable':
            return sent == {}
        if set(sent) != {'argv'} or type(sent['argv']) is not list:
            return False
        argv = sent['argv']
        if fmt == 'omitted':
            return argv == []
        if fmt == 'claude_cli' and row['backend'] == 'claude-code':
            return len(argv) == 2 and argv[0] == '--effort' and _effort(argv[1]) is not None
        if fmt == 'codex_cli' and row['backend'] == 'codex-cli':
            if len(argv) != 2 or argv[0] != '-c' or type(argv[1]) is not str:
                return False
            return any(argv[1] == 'model_reasoning_effort=' + json.dumps(e) for e in EFFORTS)
    return False


class EffortTrace:
    """Invocation-local, owned aggregation; absent delegates cannot invent history."""
    def __init__(self):
        self._count, self._complete, self._rows = 0, True, []

    def add(self, row):
        if not _valid_row(row):
            self._count, self._complete = None, False
            return
        if self._count is not None:
            self._count += 1
        self._complete = self._complete and row['format'] != 'unavailable'
        if len(self._rows) < MAX_ROWS:
            owned = deepcopy(row)
            owned['index'] = len(self._rows) + 1
            self._rows.append(owned)

    def merge(self, document, *, unknown_if_absent=True, _configured_effort=_UNSET):
        if document is None and not unknown_if_absent:
            return
        try:
            if type(document) is not dict or set(document) != DOC_KEYS or type(document['version']) is not int or document['version'] != 1:
                raise ValueError()
            count, omitted, complete, rows = (document[k] for k in ('attempt_count', 'omitted_attempts', 'complete', 'attempts'))
            if type(complete) is not bool or type(rows) is not list or len(rows) > MAX_ROWS:
                raise ValueError()
            if any(not _valid_row(row) or row['index'] != i + 1 for i, row in enumerate(rows)):
                raise ValueError()
            if count is None:
                if omitted is not None or complete:
                    raise ValueError()
            elif type(count) is not int or count < len(rows) or type(omitted) is not int or omitted != count - len(rows):
                raise ValueError()
            if complete and (omitted != 0 or any(row['format'] == 'unavailable' for row in rows)):
                raise ValueError()
            if len(json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) > MAX_BYTES:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._count, self._complete = None, False
            return
        self._count = None if self._count is None or count is None else self._count + count
        self._complete = self._complete and complete
        for row in rows[:MAX_ROWS - len(self._rows)]:
            owned = deepcopy(row)
            if _configured_effort is not _UNSET:
                # A facade may have selected a profile copy for admission. Its
                # original default is an annotation, never a new constraint or
                # a substitute for observed controls. Final serialization still
                # bounds the larger annotation after this owned copy.
                owned['configured_effort'] = _effort(_configured_effort)
            owned['index'] = len(self._rows) + 1
            self._rows.append(owned)

    def to_dict(self, *, max_bytes=MAX_BYTES):
        rows = deepcopy(self._rows)
        while True:
            omitted = None if self._count is None else self._count - len(rows)
            document = dict(version=1, attempt_count=self._count,
                complete=self._complete and omitted == 0, omitted_attempts=omitted, attempts=rows)
            if len(json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) <= min(max_bytes, MAX_BYTES):
                return document
            if not rows:
                return None
            rows.pop()


def attach(result, trace=None):
    result.metadata[KEY] = (trace if trace is not None else EffortTrace()).to_dict()
    return result
