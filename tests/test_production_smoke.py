"""Failure diagnostics for the bounded installed acceptance worker."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/production_wheel_smoke.py'


def load_smoke(monkeypatch):
    # The optional adapter is exercised in the mandatory installed-wheel gate.
    # These diagnostic units neither construct nor qualify database components.
    monkeypatch.setitem(sys.modules, 'prosaic_runtime_postgres',
                        ModuleType('prosaic_runtime_postgres'))
    spec = importlib.util.spec_from_file_location('production_smoke_fixture', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_timeout_preserves_captured_diagnostics(monkeypatch, capsys):
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, 'installed_origins', lambda: {})
    monkeypatch.setenv('PATH', os.environ.get('PATH', ''))

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 30,
            output=b'synthetic worker stage\n', stderr=b'synthetic worker traceback\n')

    monkeypatch.setattr(smoke.subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        smoke.main()
    diagnostics = capsys.readouterr().err
    assert 'synthetic worker stage' in diagnostics
    assert 'synthetic worker traceback' in diagnostics


def test_worker_watchdog_starts_before_sdk_imports(monkeypatch):
    import builtins
    import faulthandler
    observed = []
    monkeypatch.setattr(faulthandler, 'dump_traceback_later',
        lambda seconds, **kwargs: observed.append((seconds, kwargs)))
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--worker', 'fixture', 'first'])
    original_import = builtins.__import__

    def stop_at_sdk(name, *args, **kwargs):
        if name == 'prosaic':
            raise ImportError('deliberate SDK import boundary')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', stop_at_sdk)
    with pytest.raises(ImportError, match='deliberate SDK import boundary'):
        load_smoke(monkeypatch)
    assert observed == [(10, {'repeat': True})]
