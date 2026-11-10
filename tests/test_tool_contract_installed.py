import os
from pathlib import Path
import subprocess
import pytest


def test_installed_tool_contract_consumer(tmp_path):
    python = os.environ.get('PROSAIC_TOOL_CONTRACT_CANDIDATE_PYTHON')
    if python is None:
        pytest.skip('installed qualification requires the exact candidate interpreter')
    script = Path(__file__).resolve().parents[1] / 'scripts/tool_contract_smoke.py'
    result = subprocess.run([python, '-I', str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"providerRequests": 2' in result.stdout and '"toolCallbacks": 1' in result.stdout
