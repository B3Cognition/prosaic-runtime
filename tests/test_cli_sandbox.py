"""Actual tool processes, host canaries and loopback effects, not profile snapshots."""
import json
import os
from pathlib import Path
import socket
import sys

import pytest
import yaml
from prosaic_runtime import ProsaicRuntime, RuntimeConfig
from test_runtime import artifact, server, completion
from test_cli_tools import manifest, config, policy, native_call


MAC = pytest.mark.skipif(not ((sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())
                            or (sys.platform == 'linux' and Path('/usr/bin/bwrap').is_file())),
                         reason='requires Seatbelt or Bubblewrap')


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS framework interpreter')
def test_framework_python_starts_without_a_broad_prefix_grant(tmp_path):
    import sysconfig
    if not sysconfig.get_config_var('PYTHONFRAMEWORK'):
        pytest.skip('requires a framework Python build')
    import subprocess
    from types import SimpleNamespace
    from prosaic_runtime import CliSandboxConfig
    from prosaic_runtime.sandbox import sandbox_command
    with sandbox_command([sys.executable, '-c', 'print("framework-ready")'],
        cwd=tmp_path, env={}, policy=SimpleNamespace(read_roots=(), forbidden_roots=()),
        config=CliSandboxConfig(mode='required')) as (argv, env):
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'framework-ready'


def test_linux_missing_backend_stops_before_model_or_tool(server, tmp_path, monkeypatch):
    url, requests, _ = server
    tools, _, _ = manifest(tmp_path)
    import prosaic_runtime.sandbox as sandbox
    monkeypatch.setattr(sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(sandbox, 'LINUX_BACKEND', str(tmp_path / 'missing-bwrap'), raising=False)
    report = ProsaicRuntime(hardened(tmp_path, url, tools)).preflight(
        artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
    assert report['checks']['analyze_spec']['message'] == 'cli_sandbox_unavailable'
    assert requests == []


def test_linux_kernel_denial_stops_even_without_version_probe(server, tmp_path, monkeypatch):
    url, requests, _ = server
    tools, _, _ = manifest(tmp_path, version_probe=[])
    backend = tmp_path / 'bwrap'
    backend.write_text(f'#!{sys.executable}\nimport sys\n'
                       'if sys.argv[1:] == ["--version"]: print("bubblewrap 0.12.0")\n'
                       'else: sys.exit(1)\n')
    backend.chmod(0o700)
    import prosaic_runtime.sandbox as sandbox
    sandbox.sysconfig.get_paths()  # initialize for the real OS before simulating Linux
    monkeypatch.setattr(sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(sandbox, 'LINUX_BACKEND', str(backend), raising=False)
    report = ProsaicRuntime(hardened(tmp_path, url, tools)).preflight(
        artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
    assert report['checks']['analyze_spec']['message'] == 'cli_sandbox_unavailable'
    assert requests == []


def test_linux_constructs_private_readonly_mounts_and_cleans_scratch(tmp_path, monkeypatch):
    # Command contract only: real confinement is exercised by the OS tests below.
    import prosaic_runtime.sandbox as sandbox
    sandbox.sysconfig.get_paths()
    from prosaic_runtime import CliSandboxConfig
    root = tmp_path / 'workspace'
    (root / 'private').mkdir(parents=True)
    (root / 'private/secret').write_text('canary')
    backend = tmp_path / 'bwrap'
    backend.write_text(f'#!{sys.executable}\nimport sys\n'
                       'if sys.argv[1:] == ["--version"]: print("bubblewrap 0.12.0")\n')
    backend.chmod(0o700)
    monkeypatch.setattr(sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(sandbox, 'LINUX_BACKEND', str(backend), raising=False)
    with sandbox.sandbox_command([sys.executable, '-c', 'pass'], cwd=root, env={},
        policy=policy(forbidden_roots=('private', 'private/secret')), config=CliSandboxConfig(mode='required')) as (args, env):
        assert args[0] == str(backend)
        for option in ('--unshare-user', '--unshare-net', '--unshare-pid', '--unshare-ipc',
                       '--unshare-uts', '--disable-userns', '--die-with-parent', '--new-session'):
            assert option in args
        assert args[args.index('--cap-drop') + 1] == 'ALL'
        mounts = [(args[i + 1], args[i + 2]) for i, arg in enumerate(args) if arg == '--ro-bind']
        roots = [src for src, dst in mounts if dst == '/']
        assert len(roots) == 1 and Path(roots[0]).is_dir()
        assert not Path(roots[0]).is_relative_to(Path(env['TMPDIR']))
        assert (str(root), str(root)) in mounts
        assert ('/', '/') not in mounts
        masks = [src for src, dst in mounts if dst == str(root / 'private')]
        assert len(masks) == 1 and os.stat(masks[0]).st_mode & 0o777 == 0
        assert not any(dst == str(root / 'private/secret') for src, dst in mounts)
        assert not Path(masks[0]).is_relative_to(Path(env['TMPDIR']))
        home = Path(env['HOME'])
        assert home.is_dir()
    assert not home.exists() and not Path(masks[0]).exists()


def test_linux_rejects_unpatched_bubblewrap_before_model(server, tmp_path, monkeypatch):
    url, requests, _ = server
    tools, _, _ = manifest(tmp_path, version_probe=[])
    backend = tmp_path / 'bwrap'
    backend.write_text(f'#!{sys.executable}\nprint("bubblewrap 0.11.0")\n')
    backend.chmod(0o700)
    import prosaic_runtime.sandbox as sandbox
    sandbox.sysconfig.get_paths()
    monkeypatch.setattr(sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(sandbox, 'LINUX_BACKEND', str(backend), raising=False)
    report = ProsaicRuntime(hardened(tmp_path, url, tools)).preflight(
        artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
    assert report['checks']['analyze_spec']['message'] == 'cli_sandbox_unavailable'
    assert requests == []


def hardened(tmp_path, url, tools):
    cfg = config(tmp_path, url, [tools])
    path = tmp_path / 'runtime.yml'
    raw = yaml.safe_load(path.read_text())
    raw['cli_sandbox'] = {'mode': 'required'}
    path.write_text(yaml.safe_dump(raw))
    return RuntimeConfig.load(path)


def test_unsupported_required_backend_stops_before_model_or_tool(server, tmp_path, monkeypatch):
    url, requests, _ = server
    marker = tmp_path / 'executed'
    tools, _, _ = manifest(tmp_path, code=f'open({str(marker)!r}, "w").write("bad"); print("{{}}")')
    cfg = hardened(tmp_path, url, tools)
    import prosaic_runtime.sandbox as sandbox
    monkeypatch.setattr(sandbox.sys, 'platform', 'unsupported')
    rt = ProsaicRuntime(cfg)
    report = rt.preflight(artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
    assert report['checks']['analyze_spec']['message'] == 'cli_sandbox_unavailable'
    with pytest.raises(ValueError, match='preflight'):
        rt.run(artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
    assert requests == [] and not marker.exists()


@MAC
def test_sandbox_blocks_host_reads_writes_and_egress_but_preserves_input(server, tmp_path):
    url, requests, responses = server
    root = tmp_path / 'workspace'
    (root / 'evidence').mkdir(parents=True)
    (root / 'evidence/input.md').write_text('READABLE-EVIDENCE')
    private = tmp_path / 'host-secret.txt'
    private.write_text('SYNTHETIC-HOST-SECRET')
    (root / 'evidence/alias').symlink_to(private)
    marker = tmp_path / 'outside-write'
    code = f'''
import pathlib, socket
result = {{"input": pathlib.Path(sys.argv[1]).read_text()}}
for name, path, action in [
    ("secret", {str(private)!r}, "read"),
    ("alias", {str(root / 'evidence/alias')!r}, "read"),
    ("outside_write", {str(marker)!r}, "write"),
    ("workspace_write", {str(root / 'state.json')!r}, "write")]:
    try:
        if action == "read": pathlib.Path(path).read_text()
        else: pathlib.Path(path).write_text("tampered")
        result[name] = "allowed"
    except OSError as exc:
        if exc.errno not in (1, 2, 13, 30): raise
        result[name] = "denied"
result["home_exists"] = pathlib.Path(os.environ["HOME"]).is_dir()
pathlib.Path(os.environ["TMPDIR"], "cache").write_text("temporary")
try:
    socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
    result["network"] = "allowed"
except OSError: result["network"] = "denied"
print(json.dumps(result))
'''
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); listener.listen(); listener.settimeout(0.1)
        tools, _, _ = manifest(tmp_path, code=code.replace('PORT', str(listener.getsockname()[1])))
        rt = ProsaicRuntime(hardened(tmp_path, url, tools))
        responses.extend([completion('', [native_call('evidence/input.md')]), completion('done')])
        result = rt.run(artifact(['analyze_spec']), cwd=root, policy=policy(read_roots=('evidence',)))
        assert result.exit_code == 0, result.stderr
        messages = [json.loads(m['content']) for m in requests[-1]['messages'] if m['role'] == 'tool']
        assert messages == [{'status': 'ok', 'result': {
            'input': 'READABLE-EVIDENCE', 'secret': 'denied', 'alias': 'denied',
            'outside_write': 'denied', 'workspace_write': 'denied',
            'home_exists': True, 'network': 'denied'}}]
        with pytest.raises(TimeoutError):
            listener.accept()
    assert not marker.exists() and not (root / 'state.json').exists()
    assert 'SYNTHETIC-HOST-SECRET' not in json.dumps(requests)


@MAC
def test_version_probe_is_sandboxed_too(server, tmp_path):
    url, requests, _ = server
    marker = tmp_path / 'probe-write'
    tools, _, _ = manifest(tmp_path, version_probe=['--probe'], path_parameters={}, code=
        f'open({str(marker)!r}, "w").write("bad"); print("analyzer 1.0")')
    rt = ProsaicRuntime(hardened(tmp_path, url, tools))
    report = rt.preflight(artifact(['analyze_spec']), cwd=tmp_path, policy=policy(read_roots=()))
    assert report['ok'] is False and report['checks']['analyze_spec']['message'] == 'cli_version'
    assert not marker.exists() and requests == []


@pytest.mark.parametrize('value', [None, 'required', {'mode': 'auto'}, {'mode': 'off', 'network': True},
                                   {'mode': 'required', 'runtime_roots': '/'}, {'mode': 'required', 'runtime_roots': [None]}])
def test_invalid_sandbox_config_fails_closed(tmp_path, value):
    cfg = tmp_path / 'runtime.yml'
    cfg.write_text(yaml.safe_dump({'default_profile': 'local', 'profiles': {
        'local': {'base_url': 'http://localhost:9/v1', 'model': 'test'}}, 'cli_sandbox': value}))
    with pytest.raises(ValueError, match='configuration'):
        RuntimeConfig.load(cfg)


def test_off_mode_positive_control_can_read_host_and_connect(server, tmp_path):
    """Prove the canary/loopback target exists; denial is not a missing target."""
    url, requests, responses = server
    secret = tmp_path / 'host-secret.txt'
    secret.write_text('SYNTHETIC-HOST-SECRET')
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'spec.md').write_text('evidence')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); listener.listen(); listener.settimeout(1)
        tools, _, _ = manifest(tmp_path, code=
            f'import socket\nvalue = open({str(secret)!r}).read()\n'
            f'socket.create_connection(("127.0.0.1", {listener.getsockname()[1]}), timeout=0.2).close()\n'
            'print(json.dumps({"value":value}))')
        responses.extend([completion('', [native_call()]), completion('done')])
        result = ProsaicRuntime(config(tmp_path, url, [tools])).run(
            artifact(['analyze_spec']), cwd=root, policy=policy())
        assert result.exit_code == 0
        connection, _ = listener.accept()
        connection.close()
    contents = [json.loads(m['content']) for m in requests[1]['messages'] if m['role'] == 'tool']
    assert contents == [{'status': 'ok', 'result': {'value': 'SYNTHETIC-HOST-SECRET'}}]


@MAC
def test_child_process_inherits_denials_and_scratch_is_removed(server, tmp_path):
    url, requests, responses = server
    secret = tmp_path / 'host-secret.txt'
    secret.write_text('SYNTHETIC-HOST-SECRET')
    root = tmp_path / 'workspace'
    root.mkdir()
    child = f'import pathlib; pathlib.Path({str(secret)!r}).read_text()'
    tools, _, _ = manifest(tmp_path, version_probe=[], path_parameters={}, code=
        f'import subprocess\np = subprocess.run([sys.executable, "-c", {child!r}], capture_output=True)\n'
        'print(json.dumps({"child_denied": p.returncode != 0, "home": os.environ["HOME"]}))')
    responses.extend([completion('', [native_call('data')]), completion('done')])
    result = ProsaicRuntime(hardened(tmp_path, url, tools)).run(
        artifact(['analyze_spec']), cwd=root, policy=policy(read_roots=()))
    assert result.exit_code == 0
    contents = [json.loads(m['content']) for m in requests[1]['messages'] if m['role'] == 'tool']
    assert contents[0]['status'] == 'ok' and contents[0]['result']['child_denied'] is True
    assert not Path(contents[0]['result']['home']).exists()


@MAC
@pytest.mark.parametrize('forbidden', [('private',), ('private/secret.txt',), ('private', 'private/secret.txt')])
def test_forbidden_root_overrides_broad_read_grant(server, tmp_path, forbidden):
    url, requests, responses = server
    root = tmp_path / 'workspace'
    (root / 'private').mkdir(parents=True)
    secret = root / 'private/secret.txt'
    secret.write_text('SYNTHETIC-HOST-SECRET')
    (root / 'spec.md').write_text('evidence')
    tools, _, _ = manifest(tmp_path, code=
        f'try: value = open({str(secret)!r}).read()\n'
        'except (PermissionError, FileNotFoundError): value = "denied"\nprint(json.dumps({"value": value}))')
    responses.extend([completion('', [native_call()]), completion('done')])
    result = ProsaicRuntime(hardened(tmp_path, url, tools)).run(
        artifact(['analyze_spec']), cwd=root, policy=policy(forbidden_roots=forbidden))
    assert result.exit_code == 0
    contents = [json.loads(m['content']) for m in requests[1]['messages'] if m['role'] == 'tool']
    assert contents == [{'status': 'ok', 'result': {'value': 'denied'}}]
