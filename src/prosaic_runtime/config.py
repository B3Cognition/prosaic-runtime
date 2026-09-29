"""Endpoint profiles are operator configuration, independent of agent prose."""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import math
import tomllib


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
class RuntimeConfig:
    profiles: dict[str, EndpointConfig]
    routes: dict[str, str]
    default_profile: str
    allowed_tools: frozenset[str] = frozenset()

    def __post_init__(self):
        if self.default_profile not in self.profiles:
            raise ValueError("default_profile does not name a configured profile")
        if any(profile not in self.profiles for profile in self.routes.values()):
            raise ValueError("route references an unknown profile")

    @classmethod
    def load(cls, path: str | Path):
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
        return cls(
            profiles={key: EndpointConfig(**value) for key, value in raw["profiles"].items()},
            routes=raw.get("routes", {}),
            default_profile=raw["default_profile"],
            allowed_tools=frozenset(raw.get("allowed_tools", [])),
        )
