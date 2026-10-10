"""Run in a fresh three-wheel candidate environment; no DB or model dispatch."""
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import prosaic
import prosaic_runtime as runtime
import prosaic_runtime_postgres as postgres


def main():
    installed = {d.metadata["Name"].lower().replace("_", "-") for d in metadata.distributions()}
    assert {"b3-prosaic", "b3-prosaic-runtime", "b3-prosaic-runtime-postgres"} <= installed
    assert not {"prosaic", "prosaic-runtime", "prosaic-runtime-postgres"} & installed
    for package, distribution, module in (
            ("prosaic", "b3-prosaic", prosaic),
            ("prosaic_runtime", "b3-prosaic-runtime", runtime),
            ("prosaic_runtime_postgres", "b3-prosaic-runtime-postgres", postgres)):
        assert metadata.packages_distributions()[package] == [distribution]
        owner = metadata.distribution(distribution)
        assert owner.version == module.__version__
        assert "site-packages" in str(Path(module.__file__).resolve())
        assert Path(module.__file__).resolve() in {
            Path(owner.locate_file(file)).resolve() for file in owner.files}
    binary = Path(sys.executable).parent
    environment = {**os.environ, "PATH": str(binary), "PYTHONNOUSERSITE": "1"}
    assert shutil.which("node", path=environment["PATH"]) is None
    for command in ("prosaic", "prosaic-runtime"):
        result = subprocess.run([str(binary / command), "--help"], env=environment,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert command in result.stdout
    artifact = runtime.ProsaicArtifact("synthetic", "subagent", {}, "Return {{args}}.")
    config = runtime.RuntimeConfig({"offline": runtime.EndpointConfig(
        "http://127.0.0.1:9/v1", "synthetic", features={"streaming": False})},
        {}, "offline", frozenset())
    assert runtime.validate_execution_artifact(artifact, config) is None
    recorder = postgres.PostgresRecorder("postgresql://synthetic.invalid/unused",
        namespace="synthetic", environment="test",
        defaults=runtime.ExecutionContext(application_id="app"),
        rate_card=runtime.RateCard("synthetic-v1", "test-model", "2", "8", "0.5"))
    assert recorder.namespace == "synthetic" and recorder.durable
    print(json.dumps({"versions": {"core": prosaic.__version__, "runtime": runtime.__version__,
                                  "recorder": postgres.__version__},
        "checks": ["ownership", "no-legacy", "no-node", "cli-help", "pure-admission",
                   "synthetic-recorder-construction"], "database_qualified": False}))


if __name__ == "__main__":
    main()
