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
    from packaging.version import Version
    if Version(version).is_devrelease:
        # A development SDK does not authorize a publication-train update.
        assert 'b3_prosaic_runtime-0.9.22-py3-none-any.whl' in publication
        assert f'b3_prosaic_runtime-{version}-py3-none-any.whl' not in publication
    else:
        assert f'b3_prosaic_runtime-{version}-py3-none-any.whl' in publication
    assert "runpy.run_path('scripts/tool_contract_smoke.py'" in publication


def test_upload_and_release_require_explicit_success_after_skipped_ancestors():
    root=Path(__file__).resolve().parents[1]
    jobs=yaml.load((root/'.github/workflows/publish.yml').read_text(),Loader=yaml.BaseLoader)['jobs']
    assert 'always()' in jobs['pypi']['if']
    assert "needs.gate.result == 'success'" in jobs['pypi']['if']
    assert 'inputs.publish' in jobs['pypi']['if'] and "startsWith(github.ref, 'refs/tags/')" in jobs['pypi']['if']
    assert 'always()' in jobs['github-release']['if']
    assert "needs.gate.result == 'success'" in jobs['github-release']['if']
    assert "needs.pypi.result == 'success'" in jobs['github-release']['if']


def test_current_workflow_can_promote_an_immutable_qualified_tag():
    root=Path(__file__).resolve().parents[1]
    flow=yaml.load((root/'.github/workflows/publish.yml').read_text(),Loader=yaml.BaseLoader)
    assert 'release_tag' in flow['on']['workflow_dispatch']['inputs']
    for job in flow['jobs'].values():
        checkout=next(step for step in job['steps'] if str(step.get('uses','')).startswith('actions/checkout@'))
        assert 'refs/tags/{0}' in checkout['with']['ref'] and 'inputs.release_tag' in checkout['with']['ref']
        assert any('PROSAIC_RELEASE_SOURCE=$(git rev-parse HEAD)' in step.get('run','') for step in job['steps'])
    text=(root/'.github/workflows/publish.yml').read_text()
    assert '${{ github.sha }}' not in text and 'GITHUB_SHA' not in text
    assert "run['head_sha'] == os.environ['SOURCE_COMMIT']" in text
    assert 'inputs.release_tag || github.ref_name' in text


def test_hosted_promotion_contract_keeps_the_existing_sdk_matrix():
    root=Path(__file__).resolve().parents[1]
    ordinary=root/'.github/workflows'/('test.yml' if (root/'.github/workflows/test.yml').exists() else 'tests.yml')
    flow=yaml.load(ordinary.read_text(),Loader=yaml.BaseLoader)
    assert 'paths-ignore' not in flow['on']['pull_request'] if isinstance(flow['on']['pull_request'],dict) else True
    assert len(flow['jobs']['test']['strategy']['matrix']['os'])==4
    assert len(flow['jobs']['test']['strategy']['matrix']['python'])==3
    contract=yaml.load((root/'.github/workflows/promotion-checks.yml').read_text(),Loader=yaml.BaseLoader)
    jobs=contract['jobs']
    assert 'always()' in jobs['eligible-publish']['if']
    assert "needs.verified-gate.result == 'success'" in jobs['eligible-publish']['if']
    assert "needs.rejected-gate.result == 'success'" in jobs['refused-publish']['if']
    assert 'needs' in jobs['contract'] and 'always()' in jobs['contract']['if']


def test_resolved_source_uses_the_checked_out_tag_not_the_workflow_revision(tmp_path):
    import os,subprocess
    root=Path(__file__).resolve().parents[1]
    flow=yaml.load((root/'.github/workflows/publish.yml').read_text(),Loader=yaml.BaseLoader)
    step=next(step for step in flow['jobs']['artifacts']['steps'] if 'PROSAIC_RELEASE_SOURCE=$(git rev-parse HEAD)' in step.get('run',''))
    def git(*args):return subprocess.check_output(['git',*args],cwd=tmp_path,text=True).strip()
    git('init','-q');git('config','user.email','tests@example.invalid');git('config','user.name','Release test')
    payload=tmp_path/'payload';payload.write_text('qualified payload');git('add','payload');git('commit','-qm','qualified');source=git('rev-parse','HEAD');git('tag','v1.0')
    payload.write_text('new workflow');git('commit','-qam','workflow repair');workflow=git('rev-parse','HEAD')
    assert workflow!=source
    git('checkout','--detach','refs/tags/v1.0')
    output=tmp_path/'environment'
    subprocess.run(['/bin/bash','-e','-c',step['run']],cwd=tmp_path,env={**os.environ,'GITHUB_ENV':str(output),'GITHUB_SHA':workflow},check=True)
    assert output.read_text()=='PROSAIC_RELEASE_SOURCE='+source+'\n'
