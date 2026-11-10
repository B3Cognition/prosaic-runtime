"""Installed acceptance must qualify the distribution version being released."""
import ast
from pathlib import Path
import tomllib

import pytest


@pytest.mark.parametrize('script', ['production_wheel_smoke.py', 'conversation_wheel_smoke.py'])
def test_installed_ownership_check_targets_current_runtime_release(script):
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    tree = ast.parse((root / 'scripts' / script).read_text())
    expected = [node.elts[2].value for node in ast.walk(tree)
                if isinstance(node, ast.Tuple) and len(node.elts) == 3
                and isinstance(node.elts[1], ast.Constant)
                and node.elts[1].value == 'b3-prosaic-runtime'
                and isinstance(node.elts[2], ast.Constant)]
    assert expected == [version], 'installed ownership must check the current Runtime version'
