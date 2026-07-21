"""Path filtering for OpenAI-compatible provider filesystem tools."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Mapping


DEFAULT_IGNORE_GLOBS = (
    "**/.git/**",
    "**/__pycache__/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/target/**",
    "**/dist/**",
    "**/build/**",
)

DEFAULT_IGNORE_EXTENSIONS = (
    ".7z",
    ".a",
    ".bin",
    ".bmp",
    ".bz2",
    ".class",
    ".dll",
    ".dmg",
    ".dylib",
    ".ear",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".o",
    ".pdf",
    ".png",
    ".pyc",
    ".pyo",
    ".rar",
    ".so",
    ".tar",
    ".tgz",
    ".war",
    ".webp",
    ".whl",
    ".xz",
    ".zip",
)


class OpenAIPathFilter:
    def __init__(self, root: Path, features: Mapping[str, object]) -> None:
        self._root = root.resolve()
        use_defaults = _feature_bool(features, "default_ignore_filters", default=True)
        self._ignore_globs = (
            list(DEFAULT_IGNORE_GLOBS) if use_defaults else []
        ) + _feature_str_list(features, "ignore_globs")
        self._ignore_extensions = {
            _normalize_extension(value)
            for value in (
                list(DEFAULT_IGNORE_EXTENSIONS) if use_defaults else []
            )
            + _feature_str_list(features, "ignore_extensions")
        }
        self._ignore_extensions.discard("")

    def ignored(self, path: Path) -> bool:
        return self.reason(path) != ""

    def reason(self, path: Path) -> str:
        rel = self._rel(path)
        suffix = path.suffix.lower()
        if suffix in self._ignore_extensions:
            return f"extension {suffix} ignored by provider filter"
        for pattern in self._ignore_globs:
            if _matches_glob(rel, pattern):
                return f"path ignored by provider filter: {pattern}"
        return ""

    def visible_file(self, path: Path) -> bool:
        return path.is_file() and not self.ignored(path)

    def visible_tree_entry(self, path: Path) -> bool:
        return not self.ignored(path)

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve(strict=False).relative_to(self._root).as_posix()
        except ValueError:
            return path.as_posix()


def _matches_glob(rel: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    if fnmatch(rel, normalized):
        return True
    if normalized.startswith("**/") and fnmatch(rel, normalized[3:]):
        return True
    if normalized.endswith("/**"):
        directory_pattern = normalized[:-3]
        if fnmatch(rel, directory_pattern):
            return True
        if directory_pattern.startswith("**/") and fnmatch(rel, directory_pattern[3:]):
            return True
    return False


def _normalize_extension(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        return ""
    return normalized if normalized.startswith(".") else f".{normalized}"


def _feature_str_list(features: Mapping[str, object], name: str) -> list[str]:
    value = features.get(name)
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [
            item.strip()
            for item in value.split(",")
            if item.strip()
        ]
    return []


def _feature_bool(
    features: Mapping[str, object],
    name: str,
    *,
    default: bool,
) -> bool:
    value = features.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
