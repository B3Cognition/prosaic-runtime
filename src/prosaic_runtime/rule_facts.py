"""Guard-visible facts: what the model saw in a successful tool result, without bookkeeping.

Custom and CLI tools return ``{"status": "ok", "result": ...}``; the fact is the
result. Built-in tools return their data at the top level, mixed with envelope
bookkeeping (status, offsets, line counts, sizes, truncation flags, the
``N: `` line-number prefixes of file content). Each built-in has an explicit
projection that keeps only the data the model read, so bookkeeping numbers
never become business facts. Errors, refusals, skipped files and unknown
result shapes yield no fact, nor do ``write_file`` and ``edit_file``, whose only
data is the model's own input. Raw effect evidence is never changed here.
"""

import re

_NUMBERED = re.compile(r'\d+: ')


def _lines(content, *, cut=False):
    """File lines without the ``N: `` prefixes built-in readers add.

    Every line a built-in reader writes carries the prefix, so a line without
    it is a fragment cut by a character budget (or empty content) and is dropped.
    When ``cut`` says the content may have been cut by a character budget, its
    last line may be a fragment (``1500`` of ``1500000``) and is dropped too.
    """
    if type(content) is not str:
        return []
    lines = content.split('\n')
    if cut:
        lines = lines[:-1]
    return [line[match.end() :] for line in lines if (match := _NUMBERED.match(line))]


def _records(value):
    return [item for item in value if type(item) is dict] if type(value) is list else []


def _builtin(name, payload):
    get = payload.get
    if name == 'read_file':
        return {'path': get('path'), 'lines': _lines(get('content'))}
    if name == 'read_many_files':
        files = [f for f in _records(get('files')) if f.get('status') == 'ok']
        return {
            'files': [
                {'path': f.get('path'), 'lines': _lines(f.get('content'), cut=f.get('truncated') is not False)}
                for f in files
            ]
        }
    if name == 'grep_files':
        return {'matches': [{'path': m.get('path'), 'text': m.get('text')} for m in _records(get('matches'))]}
    if name == 'grep_context':
        return {
            'matches': [{'path': m.get('path'), 'lines': _lines(m.get('context'))} for m in _records(get('matches'))]
        }
    if name == 'list_files':
        matches = get('matches')
        return {'paths': [m for m in matches if type(m) is str] if type(matches) is list else []}
    if name == 'list_tree_with_sizes':
        return {'paths': [entry.get('path') for entry in _records(get('entries'))]}
    if name == 'sha256_file':
        return {'path': get('path'), 'sha256': get('sha256')}
    return None


def tool_facts(name, payload):
    """The fact a guard sees for one result, or None when the model saw no data."""
    if type(payload) is not dict or payload.get('status') != 'ok':
        return None
    if 'result' in payload:  # No built-in result has this key.
        return payload['result']  # Custom and CLI tools, including a custom tool that shadows a built-in name.
    return _builtin(name, payload)
