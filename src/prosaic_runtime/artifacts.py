"""Use Prosaic's inspection contract as the sole agent-definition parser."""
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
import hashlib
import json
import subprocess


@dataclass(frozen=True)
class ProsaicArtifact:
    id: str
    type: str
    frontmatter: dict
    body: str
    resources: tuple[dict, ...] = ()

    @classmethod
    def from_inspection(cls, data: Mapping):
        if data.get("type") not in {"subagent", "command"}:
            raise ValueError("execution requires a Prosaic subagent or command artifact")
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise ValueError("inspection artifact id is required")
        if not isinstance(data.get("frontmatter"), dict) or not isinstance(data.get("body"), str):
            raise ValueError("invalid Prosaic inspection output")
        resources = data.get("resources", [])
        if not isinstance(resources, list):
            raise ValueError("resources must be a list")
        for item in resources:
            if not isinstance(item, dict) or not isinstance(item.get("relPath"), str) or not isinstance(item.get("content"), str):
                raise ValueError("invalid bundled resource")
            path = Path(item["relPath"])
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("resource path escapes bundle")
        return cls(data["id"], data["type"], dict(data["frontmatter"]), data["body"], tuple(resources))

    def render(self, arguments: str = "") -> str:
        body = self.body.replace("{{args}}", arguments) if "{{args}}" in self.body else self.body + ("\n\n## Arguments\n" + arguments if arguments else "")
        for resource in self.resources:
            body += f"\n\n## Bundled resource: {resource['relPath']}\n{resource['content']}"
        return body

    @property
    def digest(self):
        raw = json.dumps({"id": self.id, "type": self.type, "frontmatter": self.frontmatter, "body": self.body, "resources": self.resources}, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()


def inspect_artifact(artifact_id: str, source: Path, *, executable="prosaic", timeout_s=30) -> ProsaicArtifact:
    result = subprocess.run(
        [executable, "inspect", artifact_id, "--source", str(source.resolve())],
        cwd=source.resolve().parent, capture_output=True, text=True, timeout=timeout_s, check=False,
    )
    if result.returncode:
        raise ValueError(f"Prosaic inspection failed: {result.stderr.strip()}")
    return ProsaicArtifact.from_inspection(json.loads(result.stdout))
