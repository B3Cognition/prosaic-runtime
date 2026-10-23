"""Bounded POSIX subprocess transport for native JSONL agent protocols."""
from dataclasses import dataclass
import json
import os
import selectors
import signal
import subprocess
import time


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stderr: str = ''
    failure: str | None = None
    launched: bool = False


def _stop(process):
    # Clean up the owned process group even if the leader has already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_process(argv, prompt, *, cwd, env, deadline, max_output_bytes, on_message, cancelled=None):
    if os.name != 'posix':
        raise ValueError('native execution currently requires POSIX process groups')
    if cancelled is not None and cancelled():
        return ProcessOutcome(130, failure='cancelled')
    if time.monotonic() >= deadline:
        return ProcessOutcome(1, failure='timeout')
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        return ProcessOutcome(127, 'native executable not found', 'unavailable')
    except OSError:
        return ProcessOutcome(126, 'native executable could not be started', 'startup_failure')
    pending = memoryview(prompt.encode('utf-8'))
    line = bytearray()
    errors = bytearray()
    received = 0
    failure = None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError('non-finite JSON value')

    def deliver(raw):
        try:
            event = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                               parse_constant=reject_constant)
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                return False
        except (ValueError, UnicodeError, RecursionError):
            return False
        on_message(event)
        return True

    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            if pending:
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                process.stdin.close()
            while selector.get_map():
                if cancelled is not None and cancelled():
                    failure = 'cancelled'
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = 'timeout'
                    break
                for key, _ in selector.select(min(.05, remaining)):
                    if key.data == 'stdin':
                        try:
                            count = os.write(key.fd, pending[:65536])
                            pending = pending[count:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.data == 'stdout' and line:
                            # A final JSON record may omit its trailing newline.
                            if not deliver(bytes(line)):
                                failure = 'malformed_output'
                            line.clear()
                        continue
                    received += len(chunk)
                    if received > max_output_bytes:
                        failure = 'output_limit'
                        break
                    if key.data == 'stderr':
                        errors.extend(chunk)
                    else:
                        line.extend(chunk)
                        while b'\n' in line:
                            raw, _, rest = line.partition(b'\n')
                            line[:] = rest
                            if raw.strip() and not deliver(bytes(raw)):
                                failure = 'malformed_output'
                                break
                    if failure:
                        break
                if failure:
                    break
            if not failure:
                while process.poll() is None:
                    if cancelled is not None and cancelled():
                        failure = 'cancelled'
                        break
                    if time.monotonic() >= deadline:
                        failure = 'timeout'
                        break
                    try:
                        process.wait(timeout=min(.05, max(.001, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
    finally:
        _stop(process)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
    return ProcessOutcome(130 if failure == 'cancelled' else process.returncode,
                          errors.decode('utf-8', errors='replace'), failure, True)
