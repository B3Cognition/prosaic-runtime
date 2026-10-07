"""Explicitly trusted CLI manifests. This is a subprocess adapter, not a sandbox."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import time

import yaml
from .events import check_cancelled
from .tools import CustomTool, ToolDeadlineExceeded, ToolExecutionError, depth, unique_pairs, reject_constant
from .config import CliSandboxConfig
from .sandbox import require_backend, sandbox_command


_context = ContextVar('prosaic_cli_context', default=None)
_FIELDS = {'schema_version', 'name', 'description', 'tool_version', 'executable', 'argv',
           'parameters', 'path_parameters', 'version_probe', 'version_contains', 'output_format',
           'timeout_s', 'max_output_bytes', 'pass_env', 'success_exit_codes'}


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError('duplicate or invalid key')
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _strings(value, maximum=64):
    return isinstance(value, list) and len(value) <= maximum and all(
        isinstance(v, str) and '\x00' not in v and len(v.encode()) <= 4096 for v in value)


@contextmanager
def cli_tool_context(cwd, policy, env, deadline, sandbox=CliSandboxConfig()):
    token = _context.set((Path(cwd).resolve(), policy, dict(env), deadline, sandbox))
    try:
        yield
    finally:
        _context.reset(token)


def _environment(manifest, supplied):
    # Endpoint credentials and unrelated host secrets are never inherited implicitly.
    result = {key: supplied[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR') if key in supplied}
    for key in manifest.get('pass_env', []):
        if key not in supplied:
            raise ToolExecutionError('cli_environment')
        result[key] = supplied[key]
    return result


def _kill(process):
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass


def _process(argv, *, cwd, env, timeout_s, maximum, deadline, policy, sandbox):
    with sandbox_command(argv, cwd=cwd, env=env, policy=policy, config=sandbox) as (command, environment):
        return _drain_process(command, cwd=cwd, env=environment, timeout_s=timeout_s,
                              maximum=maximum, deadline=deadline)


def _drain_process(argv, *, cwd, env, timeout_s, maximum, deadline):
    """Drain both pipes with bounded buffering; stop on cancellation or shared deadline."""
    check_cancelled()
    if time.monotonic() >= deadline:
        raise ToolDeadlineExceeded('tool invocation deadline exceeded')
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == 'posix')
    except OSError:
        raise ToolExecutionError('cli_unavailable') from None
    chunks = queue.Queue(maxsize=8)
    stopped = threading.Event()
    def read(pipe, label):
        try:
            while not stopped.is_set():
                value = pipe.read1(4096)
                while not stopped.is_set():
                    try:
                        chunks.put((label, value), timeout=0.05)
                        break
                    except queue.Full:
                        continue
                if not value:
                    break
        finally:
            pipe.close()
    readers = [threading.Thread(target=read, args=(pipe, label), daemon=True)
               for label, pipe in enumerate((process.stdout, process.stderr))]
    for reader in readers:
        reader.start()
    end = time.monotonic() + timeout_s
    total, eof, stdout = 0, 0, bytearray()
    try:
        while eof < 2:
            check_cancelled()
            now = time.monotonic()
            if now >= deadline:
                raise ToolDeadlineExceeded('tool invocation deadline exceeded')
            if now >= end:
                raise ToolExecutionError('cli_timeout')
            try:
                label, value = chunks.get(timeout=min(0.05, end - now, deadline - now))
            except queue.Empty:
                continue
            if not value:
                eof += 1
            total += len(value)
            if total > maximum:
                raise ToolExecutionError('cli_output_limit')
            if label == 0:
                stdout.extend(value)
        # A process may close its pipes but continue running.
        while process.poll() is None:
            check_cancelled()
            now = time.monotonic()
            if now >= deadline:
                raise ToolDeadlineExceeded('tool invocation deadline exceeded')
            if now >= end:
                raise ToolExecutionError('cli_timeout')
            try:
                process.wait(timeout=min(0.05, end - now, deadline - now))
            except subprocess.TimeoutExpired:
                pass
        return process.returncode, bytes(stdout)
    finally:
        stopped.set()
        _kill(process)
        process.wait()
        for reader in readers:
            reader.join(timeout=0.2)


def _path(value, root, policy):
    try:
        path = (root / value).resolve(strict=True)
        permitted = any(path.is_relative_to((root / base).resolve()) for base in policy.read_roots)
        forbidden = any(path.is_relative_to((root / base).resolve()) for base in policy.forbidden_roots)
        if not path.is_file() or not permitted or forbidden:
            raise ValueError()
        return str(path)
    except (ValueError, OSError, RuntimeError):
        raise ToolExecutionError('cli_path_denied') from None


@dataclass(frozen=True)
class CliTool:
    """Snapshot of trusted metadata; implementation changes require a tool_version bump."""
    _manifest_json: str
    executable: str | None
    version: str

    @property
    def manifest(self):
        return json.loads(self._manifest_json)

    def custom_tool(self):
        m = self.manifest
        return CustomTool(m['name'], m['description'], m['parameters'], self.execute, self.version,
                          max_result_bytes=m['max_output_bytes'] + 128)

    def preflight(self, cwd, policy, env, deadline, sandbox=CliSandboxConfig()):
        m = self.manifest
        if not self.executable or not Path(self.executable).is_file() or not os.access(self.executable, os.X_OK):
            raise ToolExecutionError('cli_unavailable')
        if m.get('path_parameters') and not policy.read_roots:
            raise ToolExecutionError('cli_path_denied')
        require_backend(sandbox)
        environment = _environment(m, env)
        if m.get('version_probe'):
            code, output = _process([self.executable, *m['version_probe']], cwd=cwd, env=environment,
                timeout_s=min(5, m['timeout_s']), maximum=m['max_output_bytes'], deadline=deadline,
                policy=policy, sandbox=sandbox)
            if code != 0 or (m.get('version_contains') and m['version_contains'].encode() not in output):
                raise ToolExecutionError('cli_version')

    def execute(self, arguments):
        context = _context.get()
        if context is None:
            raise ToolExecutionError('cli_context')
        root, policy, env, deadline, sandbox = context
        m = self.manifest
        values = {}
        for name, value in arguments.items():
            if not isinstance(value, str) or '\x00' in value or value.startswith('-'):
                raise ToolExecutionError('cli_arguments')
            values[name] = _path(value, root, policy) if name in m.get('path_parameters', {}) else value
        argv = [self.executable]
        for token in m['argv']:
            match = re.fullmatch(r'\{([A-Za-z][A-Za-z0-9_]*)\}', token)
            if match:
                if match[1] not in values:
                    raise ToolExecutionError('cli_arguments')
                argv.append(values[match[1]])
            else:
                argv.append(token)
        code, output = _process(argv, cwd=root, env=_environment(m, env), timeout_s=m['timeout_s'],
                                maximum=m['max_output_bytes'], deadline=deadline, policy=policy, sandbox=sandbox)
        if code not in m.get('success_exit_codes', [0]):
            raise ToolExecutionError('cli_exit')
        try:
            result = json.loads(output.decode('utf-8'), object_pairs_hook=unique_pairs, parse_constant=reject_constant)
            depth(result)
            return result
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ToolExecutionError('cli_invalid_output') from None


def _load(path):
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError('only regular non-symlink manifests are allowed')
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('manifest exceeds 64 KiB')
        m = yaml.load(raw.decode('utf-8'), Loader=_UniqueLoader)
        depth(m)
        if type(m) is not dict or set(m) - _FIELDS or type(m.get('schema_version')) is not int or m['schema_version'] != 1:
            raise ValueError('invalid fields or schema_version')
        executable = m.get('executable')
        if not isinstance(executable, str) or not executable or any(c in executable for c in '\x00{}'):
            raise ValueError('invalid executable')
        if not _strings(m.get('argv')):
            raise ValueError('argv must be a bounded list of strings')
        parameters = m.get('parameters', {})
        properties = parameters.get('properties', {})
        required = parameters.get('required', [])
        if not isinstance(properties, dict) or any(not isinstance(v, dict) or v.get('type') != 'string' for v in properties.values()):
            raise ValueError('CLI parameters must be strings')
        for token in m['argv']:
            if '{' in token or '}' in token:
                match = re.fullmatch(r'\{([A-Za-z][A-Za-z0-9_]*)\}', token)
                if not match or match[1] not in properties or match[1] not in required:
                    raise ValueError('argv placeholders must be whole required parameters')
        paths = m.get('path_parameters', {})
        if not isinstance(paths, dict) or any(k not in properties or v != 'read' for k, v in paths.items()):
            raise ValueError('path_parameters must name readable string parameters')
        if not _strings(m.get('version_probe', []), 8) or any('{' in s or '}' in s for s in m.get('version_probe', [])):
            raise ValueError('invalid version probe')
        if 'version_contains' in m and (not isinstance(m['version_contains'], str) or not m['version_contains'] or not m.get('version_probe')):
            raise ValueError('version_contains needs a version probe')
        passed = m.get('pass_env', [])
        if not _strings(passed) or len(set(passed)) != len(passed) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v) for v in passed):
            raise ValueError('invalid pass_env')
        codes = m.get('success_exit_codes', [0])
        if not isinstance(codes, list) or not codes or any(type(c) is not int or not 0 <= c <= 255 for c in codes):
            raise ValueError('invalid success_exit_codes')
        if m.get('output_format') != 'json':
            raise ValueError('only JSON output is supported')
        timeout = m.get('timeout_s', 30)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('timeout_s must be finite and between 0 and 3600')
        maximum = m.get('max_output_bytes', 65536)
        if type(maximum) is not int or not 128 <= maximum <= 1048576:
            raise ValueError('max_output_bytes must be between 128 and 1048576')
        m.update(timeout_s=timeout, max_output_bytes=maximum)
        version = m.get('tool_version')
        if not isinstance(version, str) or not version.strip() or len(version.encode()) > 40:
            raise ValueError('tool_version is required and limited to 40 bytes')
        found = str(path.parent / executable) if '/' in executable or '\\' in executable else shutil.which(executable)
        resolved = str(Path(found).resolve()) if found is not None else None
        canonical = json.dumps(m, sort_keys=True, allow_nan=False)
        identity = hashlib.sha256((canonical + '\n' + str(resolved)).encode()).hexdigest()
        tool = CliTool(canonical, resolved, f'{version}:{identity}')
        tool.custom_tool()  # Validate the same schema/name contract as Python callbacks.
        return tool
    except (ValueError, TypeError, AttributeError, KeyError, OSError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f'Invalid CLI tool manifest {path.name}: {type(exc).__name__}') from None


def load_cli_tools(directories):
    result = {}
    for directory in directories:
        root = Path(directory).resolve(strict=True)
        if not root.is_dir():
            raise ValueError('trusted tool directory must be a directory')
        paths = sorted(p for p in root.iterdir() if p.suffix.lower() in {'.yaml', '.yml'})
        if len(paths) + len(result) > 128:
            raise ValueError('too many CLI tool manifests')
        for path in paths:
            tool = _load(path)
            name = tool.manifest['name']
            if name in result:
                raise ValueError(f'duplicate CLI tool manifest name: {name}')
            result[name] = tool
    return result
