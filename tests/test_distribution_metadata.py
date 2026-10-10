"""Exercise the installable contracts of both clean Git archive distributions."""
import ast
from email.parser import BytesParser
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

from packaging.requirements import Requirement
import pytest


@pytest.mark.parametrize("project,name,version,package,dependency,licenses", [
    (".", "b3-prosaic-runtime", "0.8.0", "prosaic_runtime", "b3-prosaic<0.5,>=0.4",
     ("LICENSE", "LICENSE-MIT", "NOTICE.md")),
    ("adapters/postgres", "b3-prosaic-runtime-postgres", "0.2.0", "prosaic_runtime_postgres",
     "b3-prosaic-runtime<0.9,>=0.8", ("LICENSE", "LICENSE-MIT", "NOTICE.md")),
])
def test_clean_archives_ship_index_metadata_and_owned_payloads(
        tmp_path, project, name, version, package, dependency, licenses):
    root = Path(__file__).resolve().parents[1]
    revision = os.environ.get("PROSAIC_RUNTIME_RELEASE_REF", "HEAD")
    assert not revision.startswith("-")
    archive = subprocess.check_output(["git", "archive", "--format=tar", revision], cwd=root)
    source = tmp_path / "source"
    source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(source, filter="data")
    project_source = source / project
    dist = tmp_path / "dist"
    result = subprocess.run([sys.executable, "-m", "build", "--no-isolation",
                             "--outdir", str(dist), str(project_source)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    wheels, sdists = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_path = next(p for p in wheel.namelist() if p.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        assert metadata["Name"] == name
        assert metadata["Version"] == version
        assert metadata["License-Expression"] == "Apache-2.0"
        assert set(metadata.get_all("License-File")) == set(licenses)
        requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]
        assert all(requirement.url is None for requirement in requirements)
        assert str(Requirement(dependency)) in {str(requirement) for requirement in requirements}
        if project == "adapters/postgres":
            # The published 3.2.0 binary extra references an unavailable dev wheel.
            assert str(Requirement("psycopg[binary]>=3.2.1,<4")) in {
                str(requirement) for requirement in requirements}
        module_path = f"{package}/__init__.py"
        module = ast.parse(wheel.read(module_path))
        exports = {target.id: ast.literal_eval(node.value) for node in module.body
                   if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                   for target in node.targets if isinstance(target, ast.Name)}
        assert exports["__version__"] == metadata["Version"]
        for license_path in licenses:
            assert wheel.read(metadata_path.replace("METADATA", f"licenses/{license_path}")) == (
                project_source / license_path).read_bytes()
        expected = {str(path.relative_to(project_source / "src")): path.read_bytes()
                    for path in (project_source / "src" / package).rglob("*") if path.is_file()}
        assert {p: wheel.read(p) for p in wheel.namelist() if p.startswith(f"{package}/")} == expected
        metadata_files = {metadata_path.replace("METADATA", filename) for filename in (
            "METADATA", "WHEEL", "RECORD", "top_level.txt", "entry_points.txt")}
        metadata_files.update(metadata_path.replace("METADATA", f"licenses/{path}")
                              for path in licenses)
        assert set(wheel.namelist()) <= set(expected) | metadata_files
        if project == ".":
            assert b"prosaic-runtime = prosaic_runtime.cli:main" in wheel.read(
                metadata_path.replace("METADATA", "entry_points.txt"))
    with tarfile.open(sdists[0]) as sdist:
        entries = {p.name.split("/", 1)[1] for p in sdist if p.isfile()}
        assert not any(part in {".superpowers", ".tools", ".venv", ".git", "__pycache__"}
                       for entry in entries for part in Path(entry).parts)
        for path in licenses:
            assert path in entries
        archived = {str(path.relative_to(project_source)) for path in project_source.rglob("*")
                    if path.is_file() and "build" not in path.relative_to(project_source).parts
                    and not any(part.endswith(".egg-info") for part in path.parts)}
        generated = {"PKG-INFO", "setup.cfg"}
        generated.update(f"src/{name.replace('-', '_')}.egg-info/{filename}" for filename in (
            "PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
            "requires.txt", "top_level.txt"))
        assert entries <= archived | generated
        if project == ".":
            assert "docs/tool-effects.md" in entries
            assert "tests/support/tool_journal.py" in entries
