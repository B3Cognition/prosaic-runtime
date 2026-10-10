"""Build once from Git, audit release bytes, and bind actual checks to them.

This tool has no upload credentials and performs no publication. Its receipt is
build provenance; successful qualification records are separate, explicit effects.
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
import configparser
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile
import xml.etree.ElementTree as ET

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


MAX_TOTAL = 128 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_ENTRIES = 10000


class ReleaseError(ValueError):
    """Release input or evidence does not match the committed package."""


def digest(content):
    return hashlib.sha256(content).hexdigest()


def safe_path(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or
            name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/'))
            or any(ord(char) < 32 for char in name)):
        raise ReleaseError('unsafe archive path')
    return PurePosixPath(name)


def tar_contents(content):
    entries, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(content), mode='r:*') as archive:
        for index, entry in enumerate(archive):
            if index >= MAX_ENTRIES:
                raise ReleaseError('archive entry limit')
            safe_path(entry.name.rstrip('/') if entry.isdir() else entry.name)
            if entry.isdir():
                continue
            if not entry.isfile() or entry.name in entries:
                raise ReleaseError('archive contains links, special or duplicate entries')
            total += entry.size
            if entry.size > MAX_FILE or total > MAX_TOTAL:
                raise ReleaseError('archive size limit')
            entries[entry.name] = archive.extractfile(entry).read()
    return entries


def zip_contents(path):
    entries, total = {}, 0
    with zipfile.ZipFile(path) as archive:
        for index, entry in enumerate(archive.infolist()):
            if index >= MAX_ENTRIES:
                raise ReleaseError('wheel entry limit')
            safe_path(entry.filename)
            mode = entry.external_attr >> 16
            if entry.is_dir() or (mode & 0o170000) not in (0, 0o100000):
                raise ReleaseError('wheel contains nonregular entries')
            if entry.filename in entries:
                raise ReleaseError('duplicate wheel entry')
            total += entry.file_size
            if entry.file_size > MAX_FILE or total > MAX_TOTAL:
                raise ReleaseError('wheel size limit')
            entries[entry.filename] = archive.read(entry)
    return entries


def normalized_requirement(value):
    requirement = Requirement(value)
    if requirement.url:
        raise ReleaseError('package-index dependencies must not use direct URLs')
    name = canonicalize_name(requirement.name)
    extras = '[' + ','.join(sorted(canonicalize_name(extra) for extra in requirement.extras)) + ']' if requirement.extras else ''
    return name + extras + str(requirement.specifier) + (
        '; ' + str(requirement.marker) if requirement.marker else '')


def declared_dependencies(project):
    dependencies = [normalized_requirement(value) for value in project.get('dependencies', [])]
    for extra, requirements in project.get('optional-dependencies', {}).items():
        for value in requirements:
            requirement = Requirement(value)
            base = normalized_requirement(value).split(';', 1)[0]
            marker = 'extra == "' + canonicalize_name(extra) + '"'
            if requirement.marker:
                marker = '(' + str(requirement.marker) + ') and ' + marker
            dependencies.append(normalized_requirement(base + '; ' + marker))
    return sorted(dependencies)


def metadata_identity(content):
    metadata = BytesParser().parsebytes(content)
    for field in ('Name', 'Version', 'Requires-Python'):
        if len(metadata.get_all(field, [])) != 1:
            raise ReleaseError('missing or duplicate package metadata')
    dependencies = sorted(normalized_requirement(value) for value in
                          metadata.get_all('Requires-Dist', []))
    return {'name': metadata['Name'], 'version': metadata['Version'],
            'requires_python': metadata['Requires-Python'],
            'dependencies': dependencies}


def project_identity(source):
    project = tomllib.loads((source / 'pyproject.toml').read_text())['project']
    return project, {'name': project['name'], 'version': project['version'],
                     'requires_python': project['requires-python']}


def audit_pair(source, wheel, sdist, expected):
    project, identity = project_identity(source)
    if expected.get(identity['name']) != identity['version']:
        raise ReleaseError('unexpected source distribution name or version')
    source_files = {path.relative_to(source).as_posix(): path.read_bytes()
                    for path in source.rglob('*') if path.is_file()}
    archive = tar_contents(sdist.read_bytes())
    prefixes = {name.split('/')[0] for name in archive}
    if len(prefixes) != 1:
        raise ReleaseError('sdist must have one package root')
    files = {name.split('/', 1)[1]: value for name, value in archive.items() if '/' in name}
    generated = {'PKG-INFO', 'setup.cfg'}
    egg_files = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt',
                 'requires.txt', 'top_level.txt'}
    for name, content in files.items():
        if name in source_files:
            if content != source_files[name]:
                raise ReleaseError('sdist differs from committed source')
        elif name not in generated and not (
                re.fullmatch(r'src/[A-Za-z0-9_]+\.egg-info/[^/]+', name)
                and name.rsplit('/', 1)[1] in egg_files):
            raise ReleaseError('sdist contains an unapproved generated file')
    for name in source_files:
        if name.startswith('src/') and not '.egg-info/' in name and name not in files:
            raise ReleaseError('sdist omitted source payload')
    if 'pyproject.toml' not in files or 'PKG-INFO' not in files:
        raise ReleaseError('sdist missing package metadata')
    sdist_identity = metadata_identity(files['PKG-INFO'])
    contents = zip_contents(wheel)
    metadata_paths = [name for name in contents if name.endswith('.dist-info/METADATA')]
    if len(metadata_paths) != 1:
        raise ReleaseError('wheel must contain one distribution')
    info = metadata_paths[0].rsplit('/', 1)[0]
    wheel_identity = metadata_identity(contents[metadata_paths[0]])
    if wheel_identity != sdist_identity or any(wheel_identity[key] != value
                                              for key, value in identity.items()):
        raise ReleaseError('wheel and sdist identity mismatch')
    if declared_dependencies(project) != wheel_identity['dependencies']:
        raise ReleaseError('artifact dependencies differ from committed project metadata')
    allowed_info = {'METADATA', 'WHEEL', 'RECORD', 'entry_points.txt', 'top_level.txt'}
    entrypoints = configparser.ConfigParser(interpolation=None)
    entrypoints.optionxform = str
    entrypoints.read_string(contents.get(f'{info}/entry_points.txt', b'').decode())
    expected_entrypoints = {}
    for field, group in (('scripts', 'console_scripts'), ('gui-scripts', 'gui_scripts')):
        if project.get(field):
            expected_entrypoints[group] = project[field]
    expected_entrypoints.update(project.get('entry-points', {}))
    if {group: dict(entrypoints[group]) for group in entrypoints.sections()} != expected_entrypoints:
        raise ReleaseError('wheel executable entry points differ from committed source')
    licenses = project.get('license-files', [])
    if not isinstance(project.get('license'), str) or not licenses:
        raise ReleaseError('missing SPDX license or license file declaration')
    for content in (files['PKG-INFO'], contents[metadata_paths[0]]):
        metadata = BytesParser().parsebytes(content)
        if (metadata.get_all('License-Expression', []) != [project['license']]
                or sorted(metadata.get_all('License-File', [])) != sorted(licenses)):
            raise ReleaseError('artifact license metadata differs from committed SPDX declaration')
    for name in licenses:
        if name not in source_files or files.get(name) != source_files[name]:
            raise ReleaseError('sdist missing license evidence')
        if contents.get(f'{info}/licenses/{name}') != source_files[name]:
            raise ReleaseError('wheel missing license evidence')
    payload = {}
    for name, content in contents.items():
        if name.startswith(info + '/'):
            suffix = name[len(info) + 1:]
            if suffix not in allowed_info and suffix not in {'licenses/' + name for name in licenses}:
                raise ReleaseError('unexpected wheel metadata payload')
        elif source_files.get('src/' + name) != content:
            raise ReleaseError('wheel payload differs from committed source')
        else:
            payload[name] = content
    for name, content in source_files.items():
        if name.startswith('src/') and name.endswith('.py') and '.egg-info/' not in name:
            if payload.get(name[4:]) != content:
                raise ReleaseError('wheel omitted Python source')
    versions = []
    for name, content in payload.items():
        if name.endswith('/__init__.py'):
            for node in ast.parse(content).body:
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                        target.id == '__version__' for target in node.targets):
                    versions.append(ast.literal_eval(node.value))
    if identity['version'] not in versions or any(version != identity['version'] for version in versions):
        raise ReleaseError('missing or inconsistent public source version')
    record = contents.get(f'{info}/RECORD')
    if record is None:
        raise ReleaseError('missing wheel RECORD')
    seen = set()
    for name, hashed, size in csv.reader(io.StringIO(record.decode())):
        if name not in contents or name in seen:
            raise ReleaseError('wheel RECORD payload mismatch')
        seen.add(name)
        if name == f'{info}/RECORD':
            if hashed or size:
                raise ReleaseError('invalid RECORD self entry')
        else:
            encoded = base64.urlsafe_b64encode(hashlib.sha256(contents[name]).digest()).rstrip(b'=').decode()
            if hashed != 'sha256=' + encoded or size != str(len(contents[name])):
                raise ReleaseError('wheel RECORD digest mismatch')
    if seen != set(contents):
        raise ReleaseError('wheel RECORD omitted payload')
    return wheel_identity


def audit_artifacts(source, directory, expected, project_dirs=('.',)):
    artifacts = sorted(path for path in directory.iterdir()
                       if path.name.endswith(('.whl', '.tar.gz')))
    if len(artifacts) != 2 * len(expected) or len(project_dirs) != len(expected):
        raise ReleaseError('expected exactly one wheel and sdist per distribution')
    identities = []
    for relative in project_dirs:
        project_source = source if relative == '.' else source.joinpath(*safe_path(relative).parts)
        project, identity = project_identity(project_source)
        stem = identity['name'].replace('-', '_') + '-' + identity['version']
        matching_wheels = [path for path in artifacts if path.name == stem + '-py3-none-any.whl']
        matching_sdists = [path for path in artifacts if path.name == stem + '.tar.gz']
        if len(matching_wheels) != 1 or len(matching_sdists) != 1:
            raise ReleaseError('unexpected artifact filename')
        identities.append(audit_pair(project_source, matching_wheels[0], matching_sdists[0], expected))
    if {value['name'] for value in identities} != set(expected):
        raise ReleaseError('distribution set mismatch')
    return identities


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def build_release(repo, ref, output, expected, project_dirs=('.',)):
    if not ref or ref.startswith('-'):
        raise ReleaseError('invalid Git revision')
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', ref + '^{commit}'],
                                     cwd=repo, text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ReleaseError('unsupported source identity')
    contents = tar_contents(subprocess.check_output(['git', 'archive', '--format=tar', commit], cwd=repo))
    output.mkdir(parents=True, exist_ok=False)
    source, directory = output / 'source', output / 'artifacts'
    source.mkdir()
    directory.mkdir()
    for name, content in contents.items():
        target = source.joinpath(*safe_path(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    # The build backend may write generated metadata; audit against a pristine copy.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='prosaic-release-build-') as temporary:
        import shutil
        build_source = Path(temporary) / 'source'
        shutil.copytree(source, build_source)
        for relative in project_dirs:
            project_source = build_source if relative == '.' else build_source.joinpath(*safe_path(relative).parts)
            subprocess.run([sys.executable, '-m', 'build', '--no-isolation',
                            '--outdir', str(directory.resolve()), str(project_source)], check=True)
    identities = audit_artifacts(source, directory, expected, project_dirs)
    receipt = {'version': 1, 'source_commit': commit, 'distributions': identities,
               'artifacts': [{'filename': path.name, 'sha256': digest(path.read_bytes())}
                             for path in sorted(directory.iterdir())]}
    write_json(directory / 'release-receipt.json', receipt)
    (directory / 'SHA256SUMS').write_text(''.join(
        item['sha256'] + '  ' + item['filename'] + '\n' for item in receipt['artifacts']))
    return receipt


def verify_receipt(directory, source_commit):
    receipt = json.loads((directory / 'release-receipt.json').read_text())
    if receipt.get('version') != 1 or receipt.get('source_commit') != source_commit:
        raise ReleaseError('release source identity mismatch')
    artifacts = receipt.get('artifacts', [])
    if not artifacts or len({item['filename'] for item in artifacts}) != len(artifacts):
        raise ReleaseError('invalid release artifact set')
    for item in artifacts:
        name = item['filename']
        if str(safe_path(name)) != name or '/' in name or not name.endswith(('.whl', '.tar.gz')):
            raise ReleaseError('unsafe artifact filename')
        path = directory / name
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != item['sha256']:
            raise ReleaseError('release artifact digest mismatch')
    actual = {path.name for path in directory.iterdir() if path.name.endswith(('.whl', '.tar.gz'))}
    if actual != {item['filename'] for item in artifacts}:
        raise ReleaseError('unexpected release artifact set')
    return receipt


def check_existing(receipt, existing):
    if receipt != existing:
        raise ReleaseError('existing version has different release bytes; use a new version')


def validate_candidate_run(run, commit):
    if (not re.fullmatch('[0-9a-f]{40}', commit) or run.get('head_sha') != commit
            or run.get('conclusion') != 'success' or run.get('name') != 'Publish SDK'
            or run.get('path') != '.github/workflows/publish.yml'
            or run.get('event') not in ('push', 'workflow_dispatch')):
        raise ReleaseError('candidate must be a successful exact-source Publish SDK run')


def curate_upstream(directory, wheelhouse, commit, expected):
    receipt = verify_receipt(directory, commit)
    if {item['name']: item['version'] for item in receipt['distributions']} != expected:
        raise ReleaseError('unexpected upstream distributions')
    names = {name.replace('-', '_') + '-' + version + '-py3-none-any.whl'
             for name, version in expected.items()}
    source_names = {name.replace('-', '_') + '-' + version + '.tar.gz'
                    for name, version in expected.items()}
    if {item['filename'] for item in receipt['artifacts']} != names | source_names:
        raise ReleaseError('unexpected upstream artifact set')
    if {item['filename'] for item in receipt['artifacts'] if item['filename'].endswith('.whl')} != names:
        raise ReleaseError('unexpected upstream wheel set')
    wheelhouse.mkdir(parents=True, exist_ok=True)
    selected = []
    for item in receipt['artifacts']:
        if item['filename'] not in names:
            continue
        destination = wheelhouse / item['filename']
        if destination.exists() and digest(destination.read_bytes()) != item['sha256']:
            raise ReleaseError('existing curated dependency bytes changed')
        shutil.copyfile(directory / item['filename'], destination)
        selected.append(item)
    return {'source_commit': commit, 'receipt_sha256': digest(
        (directory / 'release-receipt.json').read_bytes()), 'receipt': receipt,
        'wheels': selected}


def prepare_upstream(directory, wheelhouse, candidates):
    """Download only successful approved upstream qualification artifacts."""
    import tempfile
    evidence = {'version': 1, 'candidates': []}
    for repo, run_id, commit, expected in candidates:
        if repo != 'B3Cognition/prosaic' or not run_id.isdigit():
            raise ReleaseError('explicit upstream repository and run ID required')
        run = json.loads(subprocess.check_output([
            'gh', 'api', 'repos/' + repo + '/actions/runs/' + run_id]))
        validate_candidate_run(run, commit)
        with tempfile.TemporaryDirectory(prefix='prosaic-upstream-') as temporary:
            download = Path(temporary)
            subprocess.run(['gh', 'run', 'download', run_id, '--repo', repo,
                            '--name', 'qualified-release', '--dir', str(download)], check=True)
            binding = curate_upstream(download, wheelhouse, commit, expected)
        evidence['candidates'].append({'repository': repo, 'run_id': run_id, **binding})
    path = directory / 'upstream-receipt.json'
    if path.exists() and json.loads(path.read_text()) != evidence:
        raise ReleaseError('promotion cannot replace qualified upstream dependencies')
    write_json(path, evidence)
    verify_upstream(directory, wheelhouse)
    return evidence


def verify_upstream(directory, wheelhouse):
    evidence = json.loads((directory / 'upstream-receipt.json').read_text())
    if evidence.get('version') != 1 or len(evidence.get('candidates', [])) != 1:
        raise ReleaseError('one explicit qualified Core candidate required')
    expected_sets = {'B3Cognition/prosaic': {'b3-prosaic': '0.4.1'}}
    seen, wheels = set(), {}
    for candidate in evidence['candidates']:
        repo = candidate['repository']
        if repo in seen or repo not in expected_sets or not candidate['run_id'].isdigit():
            raise ReleaseError('invalid upstream candidate set')
        seen.add(repo)
        receipt = candidate['receipt']
        serialized = (json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
        if (receipt.get('version') != 1 or receipt.get('source_commit') != candidate['source_commit']
                or not re.fullmatch('[0-9a-f]{40}', candidate['source_commit'])
                or digest(serialized) != candidate['receipt_sha256']
                or {item['name']: item['version'] for item in receipt['distributions']} != expected_sets[repo]):
            raise ReleaseError('upstream receipt identity mismatch')
        expected_names = {name.replace('-', '_') + '-' + version + '-py3-none-any.whl'
                          for name, version in expected_sets[repo].items()}
        source_names = {name.replace('-', '_') + '-' + version + '.tar.gz'
                        for name, version in expected_sets[repo].items()}
        if ({item['filename'] for item in receipt['artifacts']} != expected_names | source_names
                or len(receipt['artifacts']) != len(expected_names | source_names)):
            raise ReleaseError('unexpected upstream artifact set')
        selected = [item for item in receipt['artifacts'] if item['filename'] in expected_names]
        if len(selected) != len(expected_names) or selected != candidate['wheels']:
            raise ReleaseError('upstream wheel receipt mismatch')
        wheels.update({item['filename']: item['sha256'] for item in selected})
    if {path.name for path in wheelhouse.iterdir()} != set(wheels):
        raise ReleaseError('curated wheelhouse contains missing or unselected payload')
    for name, sha in wheels.items():
        path = wheelhouse / name
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != sha:
            raise ReleaseError('curated wheelhouse digest mismatch')
    return evidence


def upstream_digest(directory):
    path = directory / 'upstream-receipt.json'
    return digest(path.read_bytes()) if path.is_file() else None


def fetch_public(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('pypi.org', 'files.pythonhosted.org')
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ReleaseError('unexpected package-index URL')
    try:
        with urlopen(url, timeout=30) as response:
            if urlsplit(response.url).hostname not in ('pypi.org', 'files.pythonhosted.org'):
                raise ReleaseError('unexpected package-index redirect')
            content = response.read(MAX_TOTAL + 1)
            if len(content) > MAX_TOTAL:
                raise ReleaseError('package-index download size limit')
            return content
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def check_index(directory, commit, *, allow_missing=False, fetch=fetch_public):
    receipt = verify_receipt(directory, commit)
    missing = False
    for distribution in receipt['distributions']:
        name, version = distribution['name'], distribution['version']
        if not re.fullmatch('[A-Za-z0-9_.-]+', name) or not re.fullmatch('[A-Za-z0-9_.+-]+', version):
            raise ReleaseError('invalid index package identity')
        response = fetch(f'https://pypi.org/pypi/{name}/{version}/json')
        if response is None:
            if not allow_missing:
                raise ReleaseError('published distribution is missing')
            missing = True
            continue
        files = json.loads(response)['urls']
        stem = name.replace('-', '_') + '-' + version
        expected = {item['filename']: item['sha256'] for item in receipt['artifacts']
                    if item['filename'] in (stem + '-py3-none-any.whl', stem + '.tar.gz')}
        present = {item['filename'] for item in files}
        if len(files) != len(present) or not present.issubset(expected):
            raise ReleaseError('existing index version has different artifacts; use a new version')
        if present != set(expected):
            if not allow_missing:
                raise ReleaseError('published distribution is incomplete')
            missing = True
        for item in files:
            if item['digests']['sha256'] != expected[item['filename']]:
                raise ReleaseError('existing index version has different bytes; use a new version')
            parsed = urlsplit(item['url'])
            if parsed.scheme != 'https' or parsed.hostname != 'files.pythonhosted.org':
                raise ReleaseError('unexpected published artifact URL')
            content = fetch(item['url'])
            if content is None or digest(content) != expected[item['filename']]:
                raise ReleaseError('downloaded index bytes do not match qualified artifacts')
    verify_receipt(directory, commit)
    return 'publish_required' if missing else 'already_published'


def qualify(directory, commit, label, command, *, junit=None):
    if not re.fullmatch('[A-Za-z0-9_.-]{1,64}', label) or not command:
        raise ReleaseError('invalid qualification command or label')
    receipt = verify_receipt(directory, commit)
    upstream_sha256 = upstream_digest(directory)
    record_path = directory / f'qualification-{label}.json'
    record_path.unlink(missing_ok=True)
    if junit is not None:
        junit.unlink(missing_ok=True)
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise ReleaseError('qualification command failed')
    if verify_receipt(directory, commit) != receipt or upstream_digest(directory) != upstream_sha256:
        raise ReleaseError('qualification changed artifact evidence')
    record = {'version': 1, 'source_commit': commit, 'label': label,
              'command': command, 'python': sys.version.split()[0],
              'platform': sys.platform, 'machine': platform.machine(),
              'exit_code': 0, 'artifacts': receipt['artifacts']}
    if junit is not None:
        try:
            report = ET.parse(junit).getroot()
            suites, cases = list(report.iter('testsuite')), list(report.iter('testcase'))
            counts = {key: sum(int(suite.get(key, '0')) for suite in suites)
                      for key in ('tests', 'failures', 'errors', 'skipped')}
        except (OSError, ET.ParseError, ValueError) as error:
            raise ReleaseError('missing or malformed qualification result report') from error
        if (not cases or counts['tests'] != len(cases)
                or any(counts[key] for key in ('failures', 'errors', 'skipped'))
                or any(case.find(tag) is not None for case in cases
                       for tag in ('failure', 'error', 'skipped'))):
            raise ReleaseError('qualification result incomplete, failed or skipped')
        record['result_counts'] = counts
    if upstream_sha256 is not None:
        record['upstream_sha256'] = upstream_sha256
    write_json(record_path, record)
    return record


def require_qualification(directory, commit, labels):
    receipt = verify_receipt(directory, commit)
    for label in labels:
        if not re.fullmatch('[A-Za-z0-9_.-]{1,64}', label):
            raise ReleaseError('invalid qualification label')
        path = directory / f'qualification-{label}.json'
        if not path.is_file():
            raise ReleaseError('required qualification missing')
        record = json.loads(path.read_text())
        if (record.get('version') != 1 or record.get('source_commit') != commit
                or record.get('label') != label or record.get('exit_code') != 0
                or record.get('artifacts') != receipt['artifacts']
                or record.get('upstream_sha256') != upstream_digest(directory)):
            raise ReleaseError('qualification evidence mismatch')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    build = sub.add_parser('build')
    build.add_argument('--repo', type=Path, default=Path.cwd())
    build.add_argument('--ref', required=True)
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--expect', action='append', required=True, help='distribution=version')
    build.add_argument('--project', action='append', default=[])
    upstream = sub.add_parser('upstream')
    upstream.add_argument('--directory', type=Path, required=True)
    upstream.add_argument('--wheelhouse', type=Path, required=True)
    upstream.add_argument('--core-run', required=True)
    upstream.add_argument('--core-commit', required=True)
    for action in ('verify', 'qualify', 'gate', 'index-check'):
        command = sub.add_parser(action)
        command.add_argument('--directory', type=Path, required=True)
        command.add_argument('--commit', required=True)
        if action == 'qualify':
            command.add_argument('--label', required=True)
            command.add_argument('--junit', type=Path)
            command.add_argument('command', nargs=argparse.REMAINDER)
        elif action == 'gate':
            command.add_argument('--require', action='append', required=True)
        elif action == 'index-check':
            command.add_argument('--allow-missing', action='store_true')
    args = parser.parse_args()
    if args.action == 'build':
        expected = dict(value.split('=', 1) for value in args.expect)
        result = build_release(args.repo, args.ref, args.output, expected, args.project or ['.'])
    elif args.action == 'upstream':
        result = prepare_upstream(args.directory, args.wheelhouse, [
            ('B3Cognition/prosaic', args.core_run, args.core_commit, {'b3-prosaic': '0.4.1'})])
    elif args.action == 'verify':
        result = verify_receipt(args.directory, args.commit)
    elif args.action == 'qualify':
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        result = qualify(args.directory, args.commit, args.label, command, junit=args.junit)
    elif args.action == 'index-check':
        result = check_index(args.directory, args.commit, allow_missing=args.allow_missing)
    else:
        result = require_qualification(args.directory, args.commit, args.require)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
