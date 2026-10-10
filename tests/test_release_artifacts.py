"""Exercise the public release tool with committed synthetic packages."""
import importlib.util
import io
import json
from pathlib import Path
import hashlib
import base64
import subprocess
import sys
import tarfile
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/release_artifacts.py'


@pytest.fixture
def release_tool():
    spec = importlib.util.spec_from_file_location('release_artifacts', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def package_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    files = {
        'pyproject.toml': '''[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
[project]
name = "b3-release-fixture"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = ["PyYAML>=6.0"]
license = "Apache-2.0"
license-files = ["LICENSE", "NOTICE"]
[project.optional-dependencies]
test = ["pytest>=8; python_version >= '3.11'"]
[tool.setuptools.packages.find]
where = ["src"]
''',
        'src/release_fixture/__init__.py': "__version__ = '1.0.0'\n",
        'LICENSE': 'Synthetic license fixture\n',
        'NOTICE': 'Synthetic notice fixture\n',
        'MANIFEST.in': 'recursive-include docs *.md\n',
        'docs/public.md': 'Public fixture\n',
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for arguments in [('init', '-q'), ('add', '.'),
                      ('-c', 'user.name=Release Fixture', '-c',
                       'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')]:
        subprocess.run(['git', *arguments], cwd=repo, check=True,
                       capture_output=True)
    (repo / 'docs/private-untracked.md').write_text('Private untracked fixture\n')
    # Tracked working-tree modifications are excluded too.
    (repo / 'src/release_fixture/__init__.py').write_text("__version__ = '9.9.9'\n")
    return repo


@pytest.fixture
def built_release(release_tool, package_repo, tmp_path):
    output = tmp_path / 'release'
    receipt = release_tool.build_release(
        package_repo, 'HEAD', output, {'b3-release-fixture': '1.0.0'})
    return output, receipt


def test_build_uses_committed_source_only(release_tool, built_release):
    output, receipt = built_release
    assert receipt['version'] == 1
    assert len(receipt['source_commit']) == 40
    assert 'qualification' not in receipt  # A build is not test evidence.
    assert not (output / 'source/docs/private-untracked.md').exists()
    assert "'1.0.0'" in (output / 'source/src/release_fixture/__init__.py').read_text()
    assert len(receipt['artifacts']) == 2
    release_tool.verify_receipt(output / 'artifacts', receipt['source_commit'])
    with tarfile.open(next((output / 'artifacts').glob('*.tar.gz'))) as archive:
        assert not any('private-untracked' in name for name in archive.getnames())


def test_cli_builds_both_committed_projects(package_repo, tmp_path):
    adapter = package_repo / 'adapters/postgres'
    adapter.mkdir(parents=True)
    for name in ('pyproject.toml', 'LICENSE', 'NOTICE'):
        content = (package_repo / name).read_text().replace('b3-release-fixture', 'b3-adapter-fixture')
        (adapter / name).write_text(content)
    (adapter / 'src/adapter_fixture').mkdir(parents=True)
    (adapter / 'src/adapter_fixture/__init__.py').write_text("__version__ = '1.0.0'\n")
    subprocess.run(['git', 'add', 'adapters'], cwd=package_repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'adapter'], cwd=package_repo, check=True)
    result = subprocess.run([sys.executable, str(SCRIPT), 'build', '--repo', str(package_repo),
        '--ref', 'HEAD', '--output', str(tmp_path / 'release'), '--project', '.',
        '--project', 'adapters/postgres', '--expect', 'b3-release-fixture=1.0.0',
        '--expect', 'b3-adapter-fixture=1.0.0'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert len(receipt['artifacts']) == 4
    assert {item['name'] for item in receipt['distributions']} == {
        'b3-release-fixture', 'b3-adapter-fixture'}


@pytest.mark.parametrize('name', ['../escape', '/escape', 'a/../../escape',
                                'a\\escape', 'a//escape', 'a/./escape', 'C:/escape'])
def test_archive_paths_fail_closed(release_tool, name):
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode='w') as archive:
        info = tarfile.TarInfo(name)
        info.size = 1
        archive.addfile(info, io.BytesIO(b'x'))
    with pytest.raises(release_tool.ReleaseError):
        release_tool.tar_contents(archive_bytes.getvalue())


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_archive_links_fail_closed(release_tool, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        info = tarfile.TarInfo('link')
        info.type, info.linkname = kind, 'file'
        archive.addfile(info)
    with pytest.raises(release_tool.ReleaseError):
        release_tool.tar_contents(buffer.getvalue())


@pytest.mark.parametrize('change', ['metadata', 'payload', 'unexpected', 'license', 'version'])
def test_wheel_audit_rejects_mutation(release_tool, built_release, change):
    output, _ = built_release
    wheel = next((output / 'artifacts').glob('*.whl'))
    with zipfile.ZipFile(wheel) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    metadata = next(name for name in entries if name.endswith('/METADATA'))
    if change == 'metadata':
        entries[metadata] = entries[metadata].replace(b'b3-release-fixture', b'foreign-project')
    elif change == 'payload':
        entries['release_fixture/__init__.py'] += b'raise RuntimeError("changed")\n'
    elif change == 'unexpected':
        entries['unexpected.py'] = b'print("foreign payload")\n'
    elif change == 'license':
        del entries[next(name for name in entries if name.endswith('/licenses/LICENSE'))]
    else:
        entries['release_fixture/__init__.py'] = b"__version__ = '8.0.0'\n"
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    with pytest.raises(release_tool.ReleaseError):
        release_tool.audit_artifacts(output / 'source', output / 'artifacts',
                                     {'b3-release-fixture': '1.0.0'})


def test_wheel_cannot_inject_an_executable_entry_point(release_tool, built_release):
    output, _ = built_release
    wheel = next((output / 'artifacts').glob('*.whl'))
    with zipfile.ZipFile(wheel) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    info = next(name.rsplit('/', 1)[0] for name in entries if name.endswith('/METADATA'))
    entries[info + '/entry_points.txt'] = b'[console_scripts]\ninjected = release_fixture:attack\n'
    # Regenerate RECORD so payload and entry-point audits must do their own work.
    records = []
    for name, content in entries.items():
        if name.endswith('/RECORD'):
            records.append(name + ',,\n')
        else:
            sha = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
            records.append(f'{name},sha256={sha},{len(content)}\n')
    entries[info + '/RECORD'] = ''.join(records).encode()
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    with pytest.raises(release_tool.ReleaseError):
        release_tool.audit_artifacts(output / 'source', output / 'artifacts',
                                     {'b3-release-fixture': '1.0.0'})


def test_same_version_different_bytes_cannot_be_reused(release_tool, built_release):
    output, receipt = built_release
    release_tool.check_existing(receipt, receipt)
    existing = json.loads(json.dumps(receipt))
    existing['artifacts'][0]['sha256'] = '0' * 64
    with pytest.raises(release_tool.ReleaseError):
        release_tool.check_existing(receipt, existing)


def test_all_real_workflow_labels_can_qualify_and_gate(release_tool, built_release):
    import yaml
    workflow = yaml.safe_load((SCRIPT.parents[1] / '.github/workflows/publish.yml').read_text())
    native = workflow['jobs']['native']['strategy']['matrix']
    labels = [f'native-{os_name}-{version}-{kind}' for os_name in native['os']
              for version in native['python'] for kind in ('source', 'wheel')]
    postgres = workflow['jobs']['postgres']['strategy']['matrix']
    labels += [f'postgres-{os_name}-{version}' for os_name in postgres['os']
               for version in postgres['postgres']]
    labels += [f'floor-{os_name}' for os_name in postgres['os']]
    assert len(labels) == 24
    output, receipt = built_release
    for label in labels:
        release_tool.qualify(output / 'artifacts', receipt['source_commit'], label,
                             [sys.executable, '-c', 'pass'])
    release_tool.require_qualification(output / 'artifacts', receipt['source_commit'], labels)


def test_native_workflow_exposes_the_installed_cli_to_later_steps(tmp_path):
    import os
    import shutil
    import yaml
    workflow = yaml.safe_load((SCRIPT.parents[1] / '.github/workflows/publish.yml').read_text())
    environment = dict(os.environ, PATH=os.defpath, GITHUB_PATH=str(tmp_path / 'github-path'))
    executable = tmp_path / '.venv/bin/prosaic'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "installed-cli-fixture\\n"\n')
    executable.chmod(0o755)
    for step in workflow['jobs']['native']['steps']:
        script = step.get('run', '')
        if 'GITHUB_PATH' in script:
            subprocess.run(['/bin/bash', '-e', '-c', script], cwd=tmp_path,
                           env=environment, check=True)
    runner_path = tmp_path / 'github-path'
    additions = runner_path.read_text().splitlines() if runner_path.exists() else []
    environment['PATH'] = os.pathsep.join([*reversed(additions), environment['PATH']])
    assert shutil.which('prosaic', path=environment['PATH']) == str(executable)
    result = subprocess.run(['prosaic', '--version'], env=environment, check=True,
                            capture_output=True, text=True)
    assert result.stdout == 'installed-cli-fixture\n'


def test_audit_rejects_undeclared_dependency_in_matching_artifacts(release_tool, built_release):
    output, _ = built_release
    wheel = next((output / 'artifacts').glob('*.whl'))
    with zipfile.ZipFile(wheel) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    info = next(name.rsplit('/', 1)[0] for name in entries if name.endswith('/METADATA'))
    injected = b'Requires-Dist: prosaic>=0.1\n'
    metadata_path = info + '/METADATA'
    entries[metadata_path] = entries[metadata_path].replace(b'\n\n', b'\n' + injected + b'\n', 1)
    if injected not in entries[metadata_path]:
        entries[metadata_path] += injected
    records = []
    for name, content in entries.items():
        if name.endswith('/RECORD'):
            records.append(name + ',,\n')
        else:
            sha = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
            records.append(f'{name},sha256={sha},{len(content)}\n')
    entries[info + '/RECORD'] = ''.join(records).encode()
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    sdist = next((output / 'artifacts').glob('*.tar.gz'))
    archive_files = release_tool.tar_contents(sdist.read_bytes())
    with tarfile.open(sdist, 'w:gz') as archive:
        for name, content in archive_files.items():
            if name.endswith('/PKG-INFO'):
                content = content.replace(b'\n\n', b'\n' + injected + b'\n', 1)
                if injected not in content:
                    content += injected
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    with pytest.raises(release_tool.ReleaseError):
        release_tool.audit_artifacts(output / 'source', output / 'artifacts',
                                     {'b3-release-fixture': '1.0.0'})


@pytest.mark.parametrize('change', ['source', 'unexpected', 'metadata'])
def test_sdist_audit_rejects_mutation(release_tool, built_release, change):
    output, _ = built_release
    sdist = next((output / 'artifacts').glob('*.tar.gz'))
    entries = release_tool.tar_contents(sdist.read_bytes())
    prefix = next(iter(entries)).split('/')[0]
    if change == 'source':
        entries[prefix + '/src/release_fixture/__init__.py'] += b'# modified\n'
    elif change == 'unexpected':
        entries[prefix + '/credentials.json'] = b'{"synthetic":true}'
    else:
        entries[prefix + '/PKG-INFO'] = entries[prefix + '/PKG-INFO'].replace(b'1.0.0', b'2.0.0')
    with tarfile.open(sdist, 'w:gz') as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    with pytest.raises(release_tool.ReleaseError):
        release_tool.audit_artifacts(output / 'source', output / 'artifacts',
                                     {'b3-release-fixture': '1.0.0'})


def test_publication_rechecks_downloaded_index_bytes(release_tool, built_release):
    output, receipt = built_release
    responses = {}
    files = []
    for item in receipt['artifacts']:
        url = 'https://files.pythonhosted.org/' + item['filename']
        responses[url] = (output / 'artifacts' / item['filename']).read_bytes()
        files.append({'filename': item['filename'], 'digests': {'sha256': item['sha256']},
                      'url': url})
    responses['https://pypi.org/pypi/b3-release-fixture/1.0.0/json'] = json.dumps({'urls': files}).encode()
    fetch = lambda url: responses.get(url)
    assert release_tool.check_index(output / 'artifacts', receipt['source_commit'],
                                    fetch=fetch) == 'already_published'
    responses[files[0]['url']] += b'changed'
    with pytest.raises(release_tool.ReleaseError):
        release_tool.check_index(output / 'artifacts', receipt['source_commit'], fetch=fetch)
    assert release_tool.check_index(output / 'artifacts', receipt['source_commit'],
                                    allow_missing=True, fetch=lambda url: None) == 'publish_required'
    with pytest.raises(release_tool.ReleaseError):
        release_tool.check_index(output / 'artifacts', receipt['source_commit'],
                                 fetch=lambda url: None)


def test_matching_partial_index_release_can_resume(release_tool, built_release):
    output, receipt = built_release
    first = receipt['artifacts'][0]
    url = 'https://files.pythonhosted.org/' + first['filename']
    files = [{'filename': first['filename'], 'digests': {'sha256': first['sha256']}, 'url': url}]
    responses = {'https://pypi.org/pypi/b3-release-fixture/1.0.0/json':
                 json.dumps({'urls': files}).encode(),
                 url: (output / 'artifacts' / first['filename']).read_bytes()}
    fetch = lambda requested: responses.get(requested)
    assert release_tool.check_index(output / 'artifacts', receipt['source_commit'],
                                    allow_missing=True, fetch=fetch) == 'publish_required'
    with pytest.raises(release_tool.ReleaseError):
        release_tool.check_index(output / 'artifacts', receipt['source_commit'], fetch=fetch)
    files[0]['digests']['sha256'] = '0' * 64
    responses['https://pypi.org/pypi/b3-release-fixture/1.0.0/json'] = json.dumps({'urls': files}).encode()
    with pytest.raises(release_tool.ReleaseError):
        release_tool.check_index(output / 'artifacts', receipt['source_commit'],
                                 allow_missing=True, fetch=fetch)


def test_verified_artifacts_detect_missing_extra_and_changed_bytes(release_tool, built_release):
    output, receipt = built_release
    directory = output / 'artifacts'
    wheel = next(directory.glob('*.whl'))
    original = wheel.read_bytes()
    wheel.write_bytes(original + b'changed')
    with pytest.raises(release_tool.ReleaseError):
        release_tool.verify_receipt(directory, receipt['source_commit'])
    wheel.write_bytes(original)
    foreign = directory / 'foreign.whl'
    foreign.write_bytes(b'foreign')
    with pytest.raises(release_tool.ReleaseError):
        release_tool.verify_receipt(directory, receipt['source_commit'])
    foreign.unlink()
    wheel.unlink()
    with pytest.raises(release_tool.ReleaseError):
        release_tool.verify_receipt(directory, receipt['source_commit'])


def test_qualification_requires_success_and_same_bytes(release_tool, built_release):
    output, receipt = built_release
    directory = output / 'artifacts'
    commit = receipt['source_commit']
    with pytest.raises(release_tool.ReleaseError):
        release_tool.require_qualification(directory, commit, ['synthetic'])
    with pytest.raises(release_tool.ReleaseError):
        release_tool.qualify(directory, commit, 'synthetic',
                             [sys.executable, '-c', 'raise SystemExit(2)'])
    release_tool.qualify(directory, commit, 'synthetic', [sys.executable, '-c', 'pass'])
    release_tool.require_qualification(directory, commit, ['synthetic'])
    wheel = next(directory.glob('*.whl'))
    wheel.write_bytes(wheel.read_bytes() + b'mutation')
    with pytest.raises(release_tool.ReleaseError):
        release_tool.require_qualification(directory, commit, ['synthetic'])


def test_upstream_run_requires_successful_exact_source(release_tool):
    run = {'head_sha': 'a' * 40, 'conclusion': 'success', 'name': 'Publish SDK',
           'path': '.github/workflows/publish.yml', 'event': 'workflow_dispatch'}
    release_tool.validate_candidate_run(run, 'a' * 40)
    for field, value in [('head_sha', 'b' * 40), ('conclusion', 'failure'),
                         ('path', 'other.yml'), ('name', 'Tests'), ('event', 'pull_request')]:
        with pytest.raises(release_tool.ReleaseError):
            release_tool.validate_candidate_run(run | {field: value}, 'a' * 40)


def test_upstream_wheelhouse_is_explicit_and_digest_bound(release_tool, built_release, tmp_path):
    output, receipt = built_release
    wheelhouse = tmp_path / 'wheelhouse'
    wheelhouse.mkdir()
    binding = release_tool.curate_upstream(output / 'artifacts', wheelhouse,
        receipt['source_commit'], {'b3-release-fixture': '1.0.0'})
    assert [path.name for path in wheelhouse.iterdir()] == [
        'b3_release_fixture-1.0.0-py3-none-any.whl']
    assert binding['receipt_sha256'] == release_tool.digest(
        (output / 'artifacts/release-receipt.json').read_bytes())
    with pytest.raises(release_tool.ReleaseError):
        release_tool.curate_upstream(output / 'artifacts', wheelhouse,
            receipt['source_commit'], {'foreign': '1.0.0'})
    wheel = next(wheelhouse.iterdir())
    wheel.write_bytes(wheel.read_bytes() + b'changed')
    with pytest.raises(release_tool.ReleaseError):
        release_tool.curate_upstream(output / 'artifacts', wheelhouse,
            receipt['source_commit'], {'b3-release-fixture': '1.0.0'})


def test_qualification_binds_upstream_receipts(release_tool, built_release):
    output, receipt = built_release
    directory = output / 'artifacts'
    release_tool.write_json(directory / 'upstream-receipt.json', {'source': 'original'})
    record = release_tool.qualify(directory, receipt['source_commit'], 'dependency',
                                 [sys.executable, '-c', 'pass'])
    assert record['upstream_sha256'] == release_tool.digest(
        (directory / 'upstream-receipt.json').read_bytes())
    release_tool.require_qualification(directory, receipt['source_commit'], ['dependency'])
    release_tool.write_json(directory / 'upstream-receipt.json', {'source': 'changed'})
    with pytest.raises(release_tool.ReleaseError):
        release_tool.require_qualification(directory, receipt['source_commit'], ['dependency'])


def test_license_expression_is_audited(release_tool, built_release):
    output, _ = built_release
    wheel = next((output / 'artifacts').glob('*.whl'))
    with zipfile.ZipFile(wheel) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    metadata = next(name for name in entries if name.endswith('/METADATA'))
    entries[metadata] = entries[metadata].replace(b'License-Expression: Apache-2.0',
                                                 b'License-Expression: MIT')
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    with pytest.raises(release_tool.ReleaseError, match='license metadata'):
        release_tool.audit_artifacts(output / 'source', output / 'artifacts',
                                     {'b3-release-fixture': '1.0.0'})


def test_qualification_records_real_junit_counts_and_rejects_skips(
        release_tool, built_release, tmp_path):
    output, receipt = built_release
    report = tmp_path / 'cases.xml'
    command = [sys.executable, '-c',
        "from pathlib import Path; Path(" + repr(str(report)) + ").write_text(" +
        repr('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
             '<testcase name="synthetic"/></testsuite></testsuites>') + ")"]
    result = release_tool.qualify(output / 'artifacts', receipt['source_commit'],
                                  'counts', command, junit=report)
    assert result['result_counts'] == {'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 0}
    skipped_command = [sys.executable, '-c',
        "from pathlib import Path; Path(" + repr(str(report)) + ").write_text(" +
        repr('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="1">'
             '<testcase name="missing"><skipped/></testcase></testsuite></testsuites>') + ")"]
    with pytest.raises(release_tool.ReleaseError, match='skipped'):
        release_tool.qualify(output / 'artifacts', receipt['source_commit'],
                             'counts', skipped_command, junit=report)




@pytest.fixture
def curated_candidates(release_tool, tmp_path):
    directory, wheelhouse = tmp_path / 'artifacts', tmp_path / 'wheels'
    directory.mkdir(); wheelhouse.mkdir()
    filename = 'b3_prosaic-0.4.1-py3-none-any.whl'
    content = b'synthetic qualified upstream wheel'
    (wheelhouse / filename).write_bytes(content)
    artifacts = [
        {'filename': filename, 'sha256': release_tool.digest(content)},
        {'filename': 'b3_prosaic-0.4.1.tar.gz', 'sha256': release_tool.digest(b'synthetic sdist')},
    ]
    receipt = {'version': 1, 'source_commit': 'a' * 40,
               'distributions': [{'name': 'b3-prosaic', 'version': '0.4.1'}],
               'artifacts': artifacts}
    serialized = (json.dumps(receipt, sort_keys=True, indent=2) + '\n').encode()
    binding = {'repository': 'B3Cognition/prosaic', 'run_id': '123',
               'source_commit': 'a' * 40, 'receipt': receipt,
               'receipt_sha256': release_tool.digest(serialized), 'wheels': artifacts[:1]}
    evidence = {'version': 1, 'candidates': [binding]}
    release_tool.write_json(directory / 'upstream-receipt.json', evidence)
    return directory, wheelhouse, evidence


@pytest.mark.parametrize('change', ['extra', 'missing', 'digest', 'name', 'source', 'receipt', 'duplicate'])
def test_verified_wheelhouse_rejects_mixed_or_changed_candidates(
        release_tool, curated_candidates, change):
    directory, wheelhouse, evidence = curated_candidates
    release_tool.verify_upstream(directory, wheelhouse)
    if change == 'extra':
        (wheelhouse / 'prosaic-0.3.2-py3-none-any.whl').write_bytes(b'legacy')
    elif change == 'missing':
        next(wheelhouse.iterdir()).unlink()
    elif change == 'digest':
        next(wheelhouse.iterdir()).write_bytes(b'changed')
    else:
        candidate = evidence['candidates'][0]
        if change == 'name':
            candidate['receipt']['distributions'][0]['name'] = 'prosaic'
        elif change == 'source':
            candidate['source_commit'] = 'b' * 40
        elif change == 'duplicate':
            evidence['candidates'].append(candidate)
        else:
            candidate['receipt_sha256'] = '0' * 64
        release_tool.write_json(directory / 'upstream-receipt.json', evidence)
    with pytest.raises(release_tool.ReleaseError):
        release_tool.verify_upstream(directory, wheelhouse)


def test_runtime_cannot_select_a_downstream_upstream(release_tool, tmp_path):
    with pytest.raises(release_tool.ReleaseError):
        release_tool.prepare_upstream(tmp_path, tmp_path / 'wheels', [
            ('B3Cognition/prosaic-runtime', '123', 'a' * 40,
             {'b3-prosaic-runtime': '0.8.1'})])
