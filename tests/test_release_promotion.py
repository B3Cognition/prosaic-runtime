"""Promotion reuses complete exact-source qualification; it never re-infers it."""
from pathlib import Path
import tomllib
import yaml


def test_promotion_reuses_complete_exact_source_records():
    root=Path(__file__).resolve().parents[1]
    flow=yaml.load((root/'.github/workflows/publish.yml').read_text(),Loader=yaml.BaseLoader)
    assert set(flow['on'])=={'workflow_dispatch'}
    jobs=flow['jobs']
    lanes=[name for name in ('native','linux','macos','postgres') if name in jobs]
    for name in lanes:assert '!inputs.candidate_run_id' in jobs[name]['if']
    assert 'artifacts' in jobs['gate']['needs']
    assert 'always()' in jobs['gate']['if'] and "needs.artifacts.result == 'success'" in jobs['gate']['if']
    merges=[step for step in jobs['gate']['steps'] if step.get('with',{}).get('pattern')=='qualified-*']
    assert len(merges)==1 and '!inputs.candidate_run_id' in merges[0]['if']
    text=(root/'.github/workflows/publish.yml').read_text()
    assert "run['head_sha'] == os.environ['SOURCE_COMMIT']" in text
    assert 'require_qualification' in text and "run['conclusion'] == 'success'" in text


def test_public_workflows_use_current_version_and_check_tool_consumer():
    root=Path(__file__).resolve().parents[1];version=tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
    publication=(root/'.github/workflows/publish.yml').read_text()
    assert f'b3_prosaic_runtime-{version}-py3-none-any.whl' in publication
    assert "runpy.run_path('scripts/tool_contract_smoke.py'" in publication
