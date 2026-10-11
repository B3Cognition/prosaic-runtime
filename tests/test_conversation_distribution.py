"""The public installed conversation gate must execute qualified wheel APIs."""
import subprocess
import sys
from pathlib import Path

import prosaic_runtime as api


def test_public_turn_capability_has_executable_methods():
    assert 'conversation_turn_v1' in api.ProsaicRuntime.capabilities
    assert callable(api.ProsaicRuntime.prepare_turn) and callable(api.ProsaicRuntime.turn)


def test_installed_gate_rejects_source_environment_before_transport():
    script = Path(__file__).resolve().parents[1] / 'scripts/conversation_wheel_smoke.py'
    assert script.is_file(), 'installed conversation acceptance is required'
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and 'isolated' in result.stderr
