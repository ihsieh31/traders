"""Application identity and namespaced runtime paths for tradingBuffett.

This repository was forked from tradingAlpaca, but it must not read or write
the fork's environment variables or durable home-directory state.  Keep this
module dependency-light so it can be imported by configuration code early in
process startup.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping


APP_NAME = "tradingbuffett"
ENV_PREFIX = "TRADINGBUFFETT_"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_HOME = Path.home() / f".{APP_NAME}"
DEFAULT_RESULTS_DIR = APP_HOME / "results"


def app_home() -> Path:
    """Return the current user's durable tradingBuffett home directory."""
    return Path.home() / f".{APP_NAME}"


def default_results_dir() -> Path:
    """Return the default results directory for the current process."""
    return app_home() / "results"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_app_path(value, *, field: str = "path") -> Path:
    """Reject configured paths that cross out of this app's ownership roots."""
    if value is None or not str(value).strip():
        raise ValueError(f"{field} must be a non-empty path")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    resolved_path = raw.resolve(strict=False)
    allowed_roots = (
        PROJECT_ROOT.resolve(),
        app_home().resolve(),
        Path("/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    )
    if not any(_is_relative_to(resolved_path, root) for root in allowed_roots):
        roots = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(
            f"{field} must stay inside tradingBuffett or a test/temp root; "
            f"got {value!r}; allowed roots: {roots}"
        )
    # Return the non-canonical absolute spelling so callers and tests retain
    # the path they configured (not macOS's /var -> /private/var spelling).
    return raw.absolute()


_PATH_CONFIG_KEYS = (
    "results_dir",
    "memory_log_path",
    "agent_memory_dir",
    "data_cache_dir",
    "screening_selection_cache_path",
    "execution_db_path",
    "execution_lock_dir",
    "long_run_dir",
)


def validate_config_paths(config: Mapping) -> dict:
    """Return a copy of *config* with all owned paths normalized and checked."""
    normalized = dict(config or {})
    for key in _PATH_CONFIG_KEYS:
        value = normalized.get(key)
        if value:
            normalized[key] = str(validate_app_path(value, field=key))
    return normalized


def env_name(name: str) -> str:
    """Return the tradingBuffett-only environment variable for *name*."""
    normalized = str(name).strip().upper()
    if normalized.startswith(ENV_PREFIX):
        return normalized
    return f"{ENV_PREFIX}{normalized}"


def get_env(name: str, default=None):
    """Read only the namespaced tradingBuffett environment variable.

    Deliberately does not fall back to the unprefixed or tradingAlpaca names.
    This is the boundary that keeps copied projects' API credentials and
    operational settings independent.
    """
    return os.getenv(env_name(name), default)


def project_env_path() -> Path:
    """Return this checkout's explicit environment file."""
    return PROJECT_ROOT / ".env"


def load_namespaced_dotenv() -> None:
    """Load only tradingBuffett keys from this checkout's ``.env``.

    A copied checkout may still contain an old tradingAlpaca-style `.env`.
    Never inject its unprefixed keys into this process; only the explicit
    ``TRADINGBUFFETT_*`` namespace is accepted.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    for key, value in dotenv_values(project_env_path()).items():
        if key and key.startswith(ENV_PREFIX) and value is not None:
            os.environ.setdefault(key, value)


__all__ = [
    "APP_HOME",
    "APP_NAME",
    "DEFAULT_RESULTS_DIR",
    "ENV_PREFIX",
    "PROJECT_ROOT",
    "app_home",
    "default_results_dir",
    "env_name",
    "get_env",
    "load_namespaced_dotenv",
    "project_env_path",
    "validate_app_path",
    "validate_config_paths",
]
