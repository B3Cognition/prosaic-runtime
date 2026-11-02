"""Bounded native capability handshakes: no threads, prompts, or model turns."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
from threading import Lock
import time

from .effort import PROVIDER_EFFORTS
from .native_backends import native_command
from .native_process import _stop

_CACHE = {}
_LOCK = Lock()
_MAX_BYTES = 1_048_576


class MetadataCancelled(Exception):
    """Cancellation observed during metadata must not be consumed as fallback."""


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate metadata field')
        value[key] = item
    return value


def _exchange(argv, first, handle, *, cwd, env, deadline, cancelled):
    process = None
    pending = bytearray((json.dumps(first) + '\n').encode())
    line, received = bytearray(), 0
    callback_error = None
    def is_cancelled():
        nonlocal callback_error
        try:
            return cancelled is not None and cancelled()
        except Exception as exc:
            callback_error = exc
            raise
    try:
        if os.name != 'posix' or time.monotonic() >= deadline:
            return None
        if is_cancelled():
            raise MetadataCancelled()
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for pipe, kind in ((process.stdin, 'in'), (process.stdout, 'out'), (process.stderr, 'err')):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_WRITE if kind == 'in' else selectors.EVENT_READ, kind)
            while time.monotonic() < deadline:
                if is_cancelled():
                    raise MetadataCancelled()
                for key, unused in selector.select(min(.05, max(0, deadline - time.monotonic()))):
                    if key.data == 'in':
                        count = os.write(key.fd, pending[:65536])
                        del pending[:count]
                        if not pending:
                            selector.unregister(process.stdin)
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        if key.data == 'out':
                            return None
                        selector.unregister(key.fileobj)
                        continue
                    received += len(chunk)
                    if received > _MAX_BYTES:
                        return None
                    if key.data == 'err':
                        continue  # Never retain native logs/account metadata.
                    line.extend(chunk)
                    while b'\n' in line:
                        raw, unused, rest = line.partition(b'\n')
                        line[:] = rest
                        if not raw.strip():
                            continue
                        event = json.loads(raw, object_pairs_hook=_json_object)
                        if not isinstance(event, dict):
                            return None
                        replies, done, result = handle(event)
                        if done:
                            return result
                        if replies:
                            pending.extend(''.join(json.dumps(r) + '\n' for r in replies).encode())
                            if process.stdin not in [entry.fileobj for entry in selector.get_map().values()]:
                                selector.register(process.stdin, selectors.EVENT_WRITE, 'in')
        return None
    except (OSError, ValueError, TypeError, RecursionError):
        if callback_error is not None:
            raise
        return None
    finally:
        if process is not None:
            _stop(process)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


def _levels(rows, profile):
    if not isinstance(rows, list):
        return None
    if profile.backend == 'codex-cli':
        matches = [r for r in rows if isinstance(r, dict) and profile.model in (r.get('id'), r.get('model'))]
    else:
        matches = [r for r in rows if isinstance(r, dict) and r.get('value') == profile.model]
        if not matches:
            matches = [r for r in rows if isinstance(r, dict) and r.get('resolvedModel') == profile.model]
    found = []
    for row in matches:
        if profile.backend == 'codex-cli':
            values = row.get('supportedReasoningEfforts')
            if not isinstance(values, list) or any(not isinstance(v, dict) or not isinstance(v.get('reasoningEffort'), str) for v in values):
                return None
            values = [v['reasoningEffort'] for v in values]
        else:
            values = [] if row.get('supportsEffort') is False else row.get('supportedEffortLevels')
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                return None
        found.append(tuple(v for v in PROVIDER_EFFORTS[profile.backend] if v in values))
    return found[0] if found and all(v == found[0] for v in found) else None


def discover_native_efforts(profile, policy, *, cwd, env, deadline, cancelled=None):
    """Reuse metadata for 60s, scoped to profile, cwd and the complete environment."""
    if cancelled is not None and cancelled():
        raise MetadataCancelled()
    if time.monotonic() >= deadline:
        return None
    identity = json.dumps([asdict(profile), str(cwd), sorted(env.items())], sort_keys=True)
    key = hashlib.sha256(identity.encode()).digest()
    with _LOCK:
        previous = _CACHE.get(key)
        if previous is not None and previous[0] > time.monotonic():
            return previous[1]
    invocation_env = dict(env)
    if profile.config_dir:
        invocation_env['CODEX_HOME' if profile.backend == 'codex-cli' else 'CLAUDE_CONFIG_DIR'] = profile.config_dir
    deadline = min(deadline, time.monotonic() + 3)
    with tempfile.TemporaryDirectory(prefix='prosaic-effort-') as temporary:
        if profile.backend == 'codex-cli':
            argv = [profile.executable]
            if profile.native_profile:
                argv += ['--profile', profile.native_profile]
            argv += ['app-server', '--listen', 'stdio://']
            first = {'id': 0, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'prosaic_runtime', 'version': '1'}}}
            expected, rows = 0, []
            def handle(event):
                nonlocal expected
                if event.get('id') != expected:
                    return [], False, None
                if 'error' in event or not isinstance(event.get('result'), dict):
                    return [], True, None
                if expected == 0:
                    expected = 1
                    return [{'method': 'initialized'}, {'id': 1, 'method': 'model/list',
                        'params': {'limit': 100, 'includeHidden': True}}], False, None
                result = event['result']
                if not isinstance(result.get('data'), list):
                    return [], True, None
                rows.extend(result['data'])
                cursor = result.get('nextCursor')
                if cursor is None:
                    return [], True, _levels(rows, profile)
                if not isinstance(cursor, str) or expected >= 4:
                    return [], True, None
                expected += 1
                return [{'id': expected, 'method': 'model/list', 'params': {
                    'limit': 100, 'includeHidden': True, 'cursor': cursor}}], False, None
        else:
            mcp = Path(temporary) / 'mcp.json'
            mcp.write_text('{"mcpServers": {}}', encoding='utf-8')
            argv = native_command(replace(profile, effort=None), policy, mcp_file=mcp)
            argv[-1:-1] = ['--input-format', 'stream-json', '--no-session-persistence']
            first = {'type': 'control_request', 'request_id': 'effort-info',
                     'request': {'subtype': 'initialize', 'hooks': None}}
            def handle(event):
                response = event.get('response')
                if event.get('type') != 'control_response' or not isinstance(response, dict) or response.get('request_id') != 'effort-info':
                    return [], False, None
                body = response.get('response')
                levels = _levels(body.get('models'), profile) if response.get('subtype') == 'success' and isinstance(body, dict) else None
                return [], True, levels
        result = _exchange(argv, first, handle, cwd=cwd, env=invocation_env,
                           deadline=deadline, cancelled=cancelled)
    if result is not None:
        with _LOCK:
            _CACHE[key] = (time.monotonic() + 60, result)
            while len(_CACHE) > 64:
                _CACHE.pop(next(iter(_CACHE)))
    return result
