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
    # Inspection's inert enforcement entries (Core 0.5.0+), kept only when
    # inspection supplied them. Never part of the digest: tags live in body.
    rules: tuple[dict, ...] | None = None
    rule_issues: tuple[dict, ...] | None = None

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
        rules = issues = None
        if "rules" in data or "ruleIssues" in data:
            rules, issues = data.get("rules", []), data.get("ruleIssues", [])
            if not isinstance(rules, list) or not isinstance(issues, list):
                raise ValueError("inspection rules and ruleIssues must be lists")
            try:  # Private JSON copies; shapes are validated at rule admission.
                rules, issues = tuple(json.loads(json.dumps(rules))), tuple(json.loads(json.dumps(issues)))
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError("inspection rules must be JSON data") from exc
        return cls(data["id"], data["type"], dict(data["frontmatter"]), data["body"], tuple(resources),
                   rules, issues)

    def inspection(self) -> dict:
        """Owned inspection-shaped document; rule keys only when inspection had rules."""
        document = {"id": self.id, "type": self.type, "frontmatter": self.frontmatter, "body": self.body,
                    "resources": list(self.resources)}
        if self.rules or self.rule_issues:
            document.update(rules=list(self.rules or ()), ruleIssues=list(self.rule_issues or ()))
        return document

    def render(self, arguments: str = "") -> str:
        # Core's strip_tags returns an untagged text as the same object.
        from .rules import strip_for_model
        text = strip_for_model(self.body)
        body = text.replace("{{args}}", arguments) if "{{args}}" in text else text + ("\n\n## Arguments\n" + arguments if arguments else "")
        for resource in self.resources:
            content = strip_for_model(resource['content']) if resource['relPath'].lower().endswith('.md') else resource['content']
            body += f"\n\n## Bundled resource: {resource['relPath']}\n{content}"
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
