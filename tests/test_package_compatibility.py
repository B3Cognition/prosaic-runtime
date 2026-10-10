"""The installed gate is mandatory separately from the source-suite witness."""
import os
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
import pytest


def test_wheel_metadata():
    if os.environ.get("PROSAIC_RUNTIME_WHEEL_ACCEPTANCE") != "1":
        pytest.skip("requires the separately mandatory clean installed-wheel gate")
    import prosaic
    import prosaic_runtime
    assert "site-packages" in Path(prosaic_runtime.__file__).resolve().parts
    import prosaic_runtime_postgres

    for package, name, version in (
            (prosaic, "b3-prosaic", "0.4.1"),
            (prosaic_runtime, "b3-prosaic-runtime", "0.8.1"),
            (prosaic_runtime_postgres, "b3-prosaic-runtime-postgres", "0.2.1")):
        owner = metadata.distribution(name)
        assert owner.version == package.__version__ == version
        origin = Path(package.__file__).resolve()
        assert "site-packages" in origin.parts
        assert origin in {Path(owner.locate_file(file)).resolve() for file in owner.files}
        assert metadata.packages_distributions()[package.__name__] == [name]
        assert all(Requirement(raw).url is None for raw in owner.requires or [])
    requirements = {Requirement(raw).name: Requirement(raw)
                    for raw in metadata.requires("b3-prosaic-runtime")}
    assert "0.4.1" in requirements["b3-prosaic"].specifier
    assert "0.5.0" not in requirements["b3-prosaic"].specifier
    adapter = {Requirement(raw).name: Requirement(raw)
               for raw in metadata.requires("b3-prosaic-runtime-postgres")}
    assert "0.8.1" in adapter["b3-prosaic-runtime"].specifier
    assert "0.9.0" not in adapter["b3-prosaic-runtime"].specifier
    assert "3.2.1" in adapter["psycopg"].specifier
    assert "3.2.0" not in adapter["psycopg"].specifier
    for legacy in ("prosaic", "prosaic-runtime", "prosaic-runtime-postgres"):
        with pytest.raises(metadata.PackageNotFoundError):
            metadata.distribution(legacy)
    for name in ("InvocationScope", "ObserverEmitter", "ToolExecutionContext",
                 "ToolClaim", "ToolJournal", "tool_journal_descriptor",
                 "evaluate_conformance"):
        assert name in prosaic_runtime.__all__
        assert callable(getattr(prosaic_runtime, name))
