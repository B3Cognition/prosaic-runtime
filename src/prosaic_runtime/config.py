"""Endpoint profiles are operator configuration, independent of agent prose."""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import math
import yaml

CONFIG_FILENAMES = ("prosaic-runtime.yaml", "prosaic-runtime.yml")


@dataclass(frozen=True)
class RunLimits:
    timeout_s: float = 120
    max_tool_rounds: int = 8

    def __post_init__(self):
        from .policy import RunPolicy
        RunPolicy(timeout_s=self.timeout_s, max_tool_rounds=self.max_tool_rounds)


@dataclass(frozen=True)
class EndpointConfig:
    base_url: str
    model: str
    api_key_env: str | None = None
    api_key_file: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = 4096
    features: dict[str, object] = field(default_factory=dict)
    max_response_bytes: int = 8_388_608

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("base_url must be an HTTP(S) endpoint without credentials, query or fragment")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be nonempty")
        if not math.isfinite(self.temperature):
            raise ValueError("temperature must be finite")
        for name in ("max_tokens", "max_response_bytes"):
            value = getattr(self, name)
            if name == "max_tokens" and value is None:
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class CliSandboxConfig:
    """Host-selected CLI isolation. Never supplied by model/prose manifests."""
    mode: str = 'off'
    runtime_roots: tuple[str, ...] = ()

    def __post_init__(self):
        if self.mode not in {'off', 'required'}:
            raise ValueError('cli_sandbox mode must be off or required')
        if not isinstance(self.runtime_roots, tuple) or any(
            not isinstance(p, str) or not p or '\x00' in p for p in self.runtime_roots):
            raise ValueError('cli_sandbox runtime_roots must contain trusted paths')


@dataclass(frozen=True)
class RuntimeConfig:
    profiles: dict[str, EndpointConfig]
    routes: dict[str, str]
    default_profile: str
    allowed_tools: frozenset[str] = frozenset()
    limits: RunLimits = field(default_factory=RunLimits)
    tool_directories: tuple[str, ...] = ()
    cli_sandbox: CliSandboxConfig = field(default_factory=CliSandboxConfig)

    def __post_init__(self):
        if self.default_profile not in self.profiles:
            raise ValueError("default_profile does not name a configured profile")
        if any(profile not in self.profiles for profile in self.routes.values()):
            raise ValueError("route references an unknown profile")
        if not isinstance(self.tool_directories, tuple) or any(not isinstance(p, str) or not p for p in self.tool_directories):
            raise ValueError('tool_directories must contain explicit trusted directory paths')
        if not isinstance(self.cli_sandbox, CliSandboxConfig):
            raise ValueError('cli_sandbox must be a CliSandboxConfig')

    @classmethod
    def load(cls, path: str | Path | None = None):
        if path is None:
            path = next((Path(name) for name in CONFIG_FILENAMES if Path(name).exists()), None)
            if path is None:
                raise FileNotFoundError(f"No runtime configuration found; expected {' or '.join(CONFIG_FILENAMES)}")
        path = Path(path)
        if path.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError("Runtime configuration must use .yaml or .yml")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected a mapping")
            if set(raw) - {"profiles", "routes", "default_profile", "allowed_tools", "limits", "tool_directories", "cli_sandbox"}:
                raise ValueError("unknown configuration keys")
            if not isinstance(raw.get("profiles"), dict) or not isinstance(raw.get("routes", {}), dict):
                raise ValueError("profiles and routes must be mappings")
            allowed = raw.get("allowed_tools", [])
            if not isinstance(allowed, list) or not all(isinstance(tool, str) for tool in allowed):
                raise ValueError("allowed_tools must be a list of tool names")
            directories = raw.get('tool_directories', [])
            if not isinstance(directories, list) or any(not isinstance(p, str) or not p for p in directories):
                raise ValueError('tool_directories must be a list of trusted paths')
            sandbox = raw.get('cli_sandbox', {})
            if not isinstance(sandbox, dict) or set(sandbox) - {'mode', 'runtime_roots'}:
                raise ValueError('invalid cli_sandbox keys')
            roots = sandbox.get('runtime_roots', [])
            if not isinstance(roots, list) or any(not isinstance(p, str) or not p or '\x00' in p for p in roots):
                raise ValueError('cli_sandbox runtime_roots must be a list of trusted paths')
            return cls(
                profiles={key: EndpointConfig(**value) for key, value in raw["profiles"].items()},
                routes=raw.get("routes", {}),
                default_profile=raw["default_profile"],
                allowed_tools=frozenset(allowed),
                limits=RunLimits(**raw.get("limits", {})),
                tool_directories=tuple(str((path.resolve().parent / p).resolve()) for p in directories),
                cli_sandbox=CliSandboxConfig(sandbox.get('mode', 'off'),
                    tuple(str((path.resolve().parent / p).resolve()) for p in roots)),
            )
        except (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid runtime configuration {path}: {exc}") from exc
