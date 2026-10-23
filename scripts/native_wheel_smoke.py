"""Installed-wheel native acceptance with owned fixture CLIs; no inference."""
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import prosaic_runtime as api


def main():
    if not sys.flags.isolated:
        raise SystemExit('run installed acceptance with python -I')
    assert 'site-packages' in Path(api.__file__).resolve().parts
    assert metadata.version('b3-prosaic-runtime') == api.__version__
    assert 'read_receipts_v1' not in api.NativeRuntime.capabilities
    with tempfile.TemporaryDirectory(prefix='prosaic-native-acceptance-') as temporary:
        root = Path(temporary)
        def fixture(name, events, code):
            path = root / name
            trace = root / (name + '.calls')
            path.write_text(f'#!{sys.executable}\nimport sys\n'
                f'with open({str(trace)!r},"a") as f: f.write(sys.stdin.read()+"\\n")\n'
                f'print({"".join(json.dumps(e) + chr(10) for e in events)!r},end="")\n'
                f'sys.exit({code})\n', encoding='utf-8')
            path.chmod(0o755)
            return str(path)
        claude = fixture('claude', [
            {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
            {'type': 'result', 'is_error': True, 'num_turns': 0}], 1)
        codex = fixture('codex', [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'accepted'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}}], 0)
        config_file = root / 'native.yml'
        config_file.write_text(f'''kind: native
profiles:
  claude:
    backend: claude-code
    model: synthetic
    quota_key: claude-account
    executable: {claude}
  codex:
    backend: codex-cli
    model: synthetic
    quota_key: codex-account
    executable: {codex}
pools:
  coding:
    profiles: [claude, codex]
default_pool: coding
capacity_file: capacity.json
''', encoding='utf-8')
        config = api.NativeRuntimeConfig.load(config_file)
        runtime = api.NativeRuntime(config)
        artifact = api.ProsaicArtifact('synthetic', 'subagent', {}, 'Return {{args}}.')
        result = runtime.run(artifact, 'answer', cwd=root, policy=api.NativePolicy())
        assert result.exit_code == 0 and result.stdout == 'accepted'
        assert result.token_usage is None
        assert result.metadata['native_execution_v1']['known_tokens'] == 7
        assert len(result.metadata['native_execution_v1']['attempts']) == 2
        source = root / '.prosaic' / 'subagents'
        source.mkdir(parents=True)
        (source / 'test.md').write_text('---\nname: test\ndescription: Test\nexecution: agent\n---\nReturn answer.', encoding='utf-8')
        binary = Path(sys.executable).parent
        environment = {**os.environ, 'PATH': str(binary), 'PYTHONNOUSERSITE': '1'}
        completed = subprocess.run([str(binary / 'prosaic-runtime'), 'subagents/test.md',
            '--config', str(config_file), '--source', str(source.parent), '--cwd', str(root),
            '--native-permissions', 'read-only', '--quiet'], env=environment,
            capture_output=True, text=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result['metadata']['profile'] == 'codex'
        assert len(result['metadata']['native_execution_v1']['attempts']) == 1
        assert (root / 'claude.calls').read_text().count('Return') == 1
    print(json.dumps({'native_checks': ['installed-import', 'cli', 'fallback',
        'cross-process-capacity', 'unknown-usage', 'capability-separation'], 'live_inference': False}))


if __name__ == '__main__':
    main()
