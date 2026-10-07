"""OS confinement for CLI children, separate from the authenticated model client.

Backends are macOS Seatbelt and Linux Bubblewrap. Required mode never downgrades.
Python callbacks are in-process trusted code and do not go through this boundary.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import sysconfig
import tempfile
import subprocess
import re

from .tools import ToolExecutionError

LINUX_BACKEND = '/usr/bin/bwrap'


def require_backend(config):
    if config.mode != 'required':
        return
    backend = '/usr/bin/sandbox-exec' if sys.platform == 'darwin' else LINUX_BACKEND
    if sys.platform not in {'darwin', 'linux'} or not Path(backend).is_file() or not os.access(backend, os.X_OK):
        raise ToolExecutionError('cli_sandbox_unavailable')
    if sys.platform == 'linux':
        # Availability means kernel confinement works, not merely bwrap exists.
        # Probe trusted Python, never the manifest executable or endpoint key.
        from types import SimpleNamespace
        try:
            version = subprocess.run([backend, '--version'], stdin=subprocess.DEVNULL,
                capture_output=True, timeout=5, env={'PATH': '/usr/bin:/bin'})
            match = re.fullmatch(rb'bubblewrap (\d+)\.(\d+)\.(\d+)\s*', version.stdout)
            if version.returncode != 0 or not match or tuple(map(int, match.groups())) < (0, 12, 0):
                # GHSA-pxhw-h44j-8pfx: unsafe setup on attacker-controlled trees.
                raise ToolExecutionError('cli_sandbox_unavailable')
            with tempfile.TemporaryDirectory(prefix='prosaic-sandbox-check-') as temporary:
                scratch = Path(temporary).resolve()
                args = _linux_command([sys.executable, '-c', 'pass'], '/',
                    SimpleNamespace(read_roots=(), forbidden_roots=()),
                    SimpleNamespace(runtime_roots=()), scratch, scratch)
                result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=5,
                    env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
                if result.returncode != 0:
                    raise ToolExecutionError('cli_sandbox_unavailable')
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError('cli_sandbox_unavailable') from exc


def _linux_command(argv, cwd, policy, config, scratch, masks):
    """Empty mount namespace: read-only dependencies/evidence, private RW scratch.

    No architecture-specific loader paths. Ubuntu/Debian merged-/usr aliases
    are recreated, while both venv and resolved interpreter names are mounted.
    Masks live outside writable scratch so a child cannot chmod the backing inode.
    """
    args = [LINUX_BACKEND, '--unshare-user', '--unshare-net', '--unshare-pid',
            '--unshare-ipc', '--unshare-uts', '--disable-userns', '--cap-drop', 'ALL',
            '--die-with-parent', '--new-session']
    rootfs = masks / 'rootfs'
    rootfs.mkdir()
    def placeholder(destination, directory):
        path = rootfs / Path(destination).relative_to('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        if directory:
            path.mkdir(exist_ok=True)
        else:
            path.touch(exist_ok=True)
    args.extend(['--ro-bind', str(rootfs), '/'])
    libraries = [sysconfig.get_path(k) for k in ('stdlib', 'platstdlib', 'purelib', 'platlib')]
    reads = [Path('/usr/lib'), Path('/lib'), Path('/lib64'), Path('/etc/ld.so.cache'),
             Path(sys.base_prefix) / 'lib', Path(sys.prefix) / 'pyvenv.cfg',
             Path(sys.executable), Path(sys.executable).resolve(), Path(argv[0]),
             *config.runtime_roots, *(Path(cwd) / p for p in policy.read_roots),
             *(Path(p) for p in libraries if p)]
    mounts = set()
    for item in reads:
        path = Path(item)
        if path.exists():
            # Resolve directory symlinks and recreate loader aliases below;
            # file aliases stay at their original name (venv interpreter).
            destination = path.resolve() if path.is_dir() else path.absolute()
            mounts.add((str(path.resolve()), str(destination)))
    for source, destination in sorted(mounts, key=lambda pair: (len(Path(pair[1]).parts), pair[1])):
        placeholder(destination, Path(source).is_dir())
        args.extend(['--ro-bind', source, destination])
    for alias in ('/lib', '/lib64'):
        path = Path(alias)
        if path.is_symlink():
            (rootfs / alias.lstrip('/')).symlink_to(str(path.resolve()))
    placeholder(str(Path(cwd).resolve()), True)
    placeholder(str(scratch), True)
    args.extend(['--bind', str(scratch), str(scratch)])
    # Overlay forbidden paths after broad read grants. Skip absent/invisible
    # paths; no host path is made readable merely to install its deny mask.
    denied = []
    for index, item in enumerate(sorted(policy.forbidden_roots, key=lambda item: len((Path(cwd) / item).resolve().parts))):
        target = (Path(cwd) / item).resolve()
        if any(target.is_relative_to(parent) for parent in denied):
            continue
        if target.exists() and any(target == Path(dst) or
            (Path(src).is_dir() and target.is_relative_to(Path(dst))) for src, dst in mounts):
            mask = masks / str(index)
            if target.is_dir():
                mask.mkdir(mode=0o000)
            else:
                mask.touch(mode=0o000)
            args.extend(['--ro-bind', str(mask), str(target)])
            denied.append(target)
    placeholder('/dev/null', False)
    placeholder('/dev/urandom', False)
    # Root is read-only, including empty cwd/ancestors; only scratch is writable.
    # No host /proc or /dev tree (including terminal/IPC sockets) is exposed.
    # --dev-bind permits these specific devices; ordinary binds are nodev.
    args.extend(['--dev-bind', '/dev/null', '/dev/null',
                 '--dev-bind', '/dev/urandom', '/dev/urandom', '--remount-ro', '/dev/urandom',
                 '--chdir', str(Path(cwd).resolve()), '--', *argv])
    return args


def _scope(path):
    path = Path(path).resolve()
    kind = 'subpath' if path.is_dir() else 'literal'
    return f'({kind} {json.dumps(str(path))})'


def _profile(argv, cwd, policy, config, scratch):
    # Runtime/library directories are trusted dependencies, not the caller's
    # home directory. A CLI with additional dependencies must name narrow roots.
    libraries = [sysconfig.get_path(k) for k in ('stdlib', 'platstdlib', 'purelib', 'platlib')]
    reads = [Path('/System/Library'), Path('/usr/lib'), Path(sys.executable).resolve(),
             Path(sys.base_prefix) / 'lib', Path(sys.prefix) / 'pyvenv.cfg',
             Path(argv[0]), *config.runtime_roots,
             *(Path(cwd) / p for p in policy.read_roots), *(p for p in libraries if p)]
    lines = ['(version 1)', '(deny default)', '(allow process-exec)', '(allow process-fork)',
             '(allow process-info* (target self))', '(allow signal (target same-sandbox))',
             '(allow sysctl-read)', '(allow file-read-metadata)',
             # dyld opens the filesystem root during startup; literal is not
             # recursive and does not grant access to its child file contents.
             '(allow file-read* (literal "/"))',
             '(allow file-read* (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))',
             '(allow file-write* (literal "/dev/null"))']
    lines.extend(f'(allow file-read* {_scope(p)})' for p in reads if Path(p).exists())
    lines.extend([f'(allow file-read* {_scope(scratch)})', f'(allow file-write* {_scope(scratch)})'])
    # Explicit denies override broad evidence/library grants. CLI manifests have
    # read path arguments only; all non-scratch writes remain denied.
    for p in policy.forbidden_roots:
        path = str((Path(cwd) / p).resolve())
        lines.append(f'(deny file-read* file-write* (literal {json.dumps(path)}) (subpath {json.dumps(path)}))')
    # No network or IPC permission is granted, including loopback/Unix sockets.
    return '\n'.join(lines)


@contextmanager
def sandbox_command(argv, *, cwd, env, policy, config):
    require_backend(config)
    if config.mode == 'off':
        yield argv, env
        return
    with tempfile.TemporaryDirectory(prefix='prosaic-cli-sandbox-') as temporary:
        scratch = Path(temporary).resolve()
        home = scratch / 'home'
        home.mkdir(mode=0o700)
        environment = dict(env)
        environment.update(HOME=str(home), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch),
                           PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        if sys.platform == 'linux':
            with tempfile.TemporaryDirectory(prefix='prosaic-cli-deny-') as temporary_masks:
                yield _linux_command(argv, cwd, policy, config, scratch,
                                     Path(temporary_masks).resolve()), environment
        else:
            yield ['/usr/bin/sandbox-exec', '-p', _profile(argv, cwd, policy, config, scratch), *argv], environment
