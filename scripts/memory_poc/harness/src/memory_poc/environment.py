from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .constants import PROXY_AND_TLS_ENV_KEYS, REQUIRED_PROVIDER_ENV_KEYS
from .errors import ConfigurationError, HarnessError
from .paths import ensure_owner_directory, harness_root, runtime_root, workspace_root

_DOTENV_ASSIGNMENT = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_PLACEHOLDER = "REPLACE_ME"
_FALLBACK_WORKTREE = Path("/Users/rk/work/chainbot/avibe-bot/avibe")
# Literal matching below this floor is too noisy against fixed report and summary text.
SECRET_SUBSTRING_MATCH_MIN_LENGTH = 16


@dataclass(frozen=True)
class ProviderSettings:
    llm_base_url: str = field(repr=False)
    llm_model: str
    llm_api_key: str = field(repr=False)
    embedding_base_url: str = field(repr=False)
    embedding_model: str
    embedding_api_key: str = field(repr=False)
    source: Path

    def endpoint_locality(self) -> str:
        hosts = {
            (urlparse(value).hostname or "").lower()
            for value in (self.llm_base_url, self.embedding_base_url)
        }
        loopback_hosts = {"localhost", "127.0.0.1", "::1"}
        return "loopback" if hosts and hosts.issubset(loopback_hosts) else "remote"


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse the small frozen `.env.poc` surface without exporting values."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DOTENV_ASSIGNMENT.match(line)
        if match is None:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    return result


def dotenv_candidates(root: Path | None = None) -> tuple[Path, Path]:
    checkout = checked_workspace_root(root)
    return (
        checkout / ".runtime" / "memory-poc" / ".env.poc",
        _FALLBACK_WORKTREE / ".runtime" / "memory-poc" / ".env.poc",
    )


def discover_provider_settings(root: Path | None = None) -> ProviderSettings:
    checkout = checked_workspace_root(root)
    local_candidate, fallback_candidate = dotenv_candidates(checkout)
    for candidate in (local_candidate, fallback_candidate):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        if candidate == local_candidate:
            _assert_anchored_parent(candidate.parent, anchor=checkout)
        else:
            _assert_anchored_parent(candidate.parent, anchor=_FALLBACK_WORKTREE)
        _assert_dotenv_safe(candidate)
        values = parse_dotenv(candidate.read_text(encoding="utf-8"))
        return _settings_from_values(values, candidate)
    raise ConfigurationError("provider_configuration_missing")


def _assert_dotenv_safe(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigurationError("provider_configuration_file_unsafe")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigurationError("provider_configuration_owner_invalid")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ConfigurationError("provider_configuration_mode_invalid")


def _assert_anchored_parent(path: Path, *, anchor: Path) -> None:
    try:
        anchor_info = anchor.lstat()
    except FileNotFoundError as exc:
        raise ConfigurationError("provider_configuration_path_unsafe") from exc
    if stat.S_ISLNK(anchor_info.st_mode) or not stat.S_ISDIR(anchor_info.st_mode):
        raise ConfigurationError("provider_configuration_path_unsafe")
    if hasattr(os, "getuid") and anchor_info.st_uid != os.getuid():
        raise ConfigurationError("provider_configuration_owner_invalid")
    try:
        components = path.relative_to(anchor).parts
    except ValueError as exc:
        raise ConfigurationError("provider_configuration_path_unsafe") from exc
    current = anchor
    for component in components:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise ConfigurationError("provider_configuration_path_unsafe") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ConfigurationError("provider_configuration_path_unsafe")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ConfigurationError("provider_configuration_owner_invalid")


def _settings_from_values(values: dict[str, str], source: Path) -> ProviderSettings:
    missing = [key for key in REQUIRED_PROVIDER_ENV_KEYS if values.get(key, "").strip() in {"", _PLACEHOLDER}]
    if missing:
        raise ConfigurationError("provider_configuration_incomplete:" + ",".join(missing))
    _assert_model_names_do_not_contain_keys(values)
    return ProviderSettings(
        llm_base_url=values["LLM_BASE_URL"],
        llm_model=values["LLM_MODEL"],
        llm_api_key=values["LLM_API_KEY"],
        embedding_base_url=values["EMBEDDING_BASE_URL"],
        embedding_model=values["EMBEDDING_MODEL"],
        embedding_api_key=values["EMBEDDING_API_KEY"],
        source=source,
    )


def _assert_model_names_do_not_contain_keys(values: dict[str, str]) -> None:
    model_values = (values["LLM_MODEL"], values["EMBEDDING_MODEL"])
    key_values = matchable_secret_values((values["LLM_API_KEY"], values["EMBEDDING_API_KEY"]))
    if any(key in model for model in model_values for key in key_values):
        raise ConfigurationError("provider_model_contains_secret")


def matchable_secret_values(values: Iterable[str]) -> tuple[str, ...]:
    """Return deduplicated secrets safe for literal checks in identity fields only.

    Short values are deliberately excluded: they occur in fixed report prose and
    cannot be distinguished reliably from non-secret text.
    """
    return tuple(dict.fromkeys(value for value in values if len(value) >= SECRET_SUBSTRING_MATCH_MIN_LENGTH))


def locked_environment_python(root: Path | None = None) -> Path:
    return runtime_root(root) / "env" / "bin" / "python"


def lock_id() -> str:
    lock = harness_root() / "uv.lock"
    if not lock.is_file():
        raise HarnessError("dependency_lock_missing")
    return hashlib.sha256(lock.read_bytes()).hexdigest()[:16]


def sync_locked_environment(*, root: Path | None = None, uv_binary: str = "uv") -> Path:
    """Create the POC-owned Python 3.12 environment from the committed lock."""
    checkout = checked_workspace_root(root)
    state = ensure_owner_directory(runtime_root(checkout), anchor=checkout)
    environment = state / "env"
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "UV_CACHE_DIR": str(state / "uv-cache"),
        "UV_PROJECT_ENVIRONMENT": str(environment),
    }
    subprocess.run(
        [
            uv_binary,
            "sync",
            "--locked",
            "--group",
            "dev",
            "--project",
            str(harness_root()),
            "--python",
            "3.12",
        ],
        check=True,
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    verify_locked_environment(environment / "bin" / "python")
    return environment / "bin" / "python"


def verify_locked_environment(python: Path | None = None) -> Path:
    """Reject host Python, user-site packages, and a lock-inconsistent closure."""
    target = python or locked_environment_python()
    if not target.is_file() or not os.access(target, os.X_OK):
        raise HarnessError("locked_environment_missing")
    lock_path = harness_root() / "uv.lock"
    result = subprocess.run(
        [str(target), "-m", "memory_poc.lock_check", str(lock_path)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": f"{target.parent}:/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
    )
    if result.returncode != 0:
        raise HarnessError("locked_environment_verification_failed")
    prefix = Path(result.stdout.strip()).resolve()
    expected = target.parent.parent.resolve()
    if prefix != expected:
        raise HarnessError("locked_environment_prefix_mismatch")
    return target


def verify_harness_interpreter(root: Path | None = None) -> Path:
    """Require the coordinator itself to use this run's locked interpreter."""
    checkout = checked_workspace_root(root)
    expected = locked_environment_python(checkout)
    actual = Path(sys.executable)
    expected_prefix = expected.parent.parent.resolve()
    if Path(sys.prefix).resolve() != expected_prefix or actual.resolve() != expected.resolve():
        raise HarnessError("harness_interpreter_not_locked")
    return verify_locked_environment(expected)


def assert_clean_harness_source(root: Path | None = None) -> None:
    """A live report may identify only a committed harness revision."""
    checkout = checked_workspace_root(root)
    result = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "scripts/memory_poc/harness",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HarnessError("harness_source_identity_unavailable")
    if result.stdout.strip():
        raise HarnessError("harness_source_dirty")


def child_environment(
    settings: ProviderSettings,
    *,
    python: Path,
    everos_root: Path,
    child_home: Path,
    metrics_path: Path,
    owner_id: str,
    anchor: Path | None = None,
) -> dict[str, str]:
    """Build the only environment inherited by the provider child process."""
    if not owner_id:
        raise HarnessError("fixed_owner_missing")
    ensure_owner_directory(child_home, anchor=anchor)
    xdg_config = ensure_owner_directory(child_home / ".config", anchor=child_home)
    xdg_data = ensure_owner_directory(child_home / ".local" / "share", anchor=child_home)
    xdg_cache = ensure_owner_directory(child_home / ".cache", anchor=child_home)
    xdg_state = ensure_owner_directory(child_home / ".local" / "state", anchor=child_home)
    env = {
        "ENV": "prod",
        "EVEROS_EMBEDDING__API_KEY": settings.embedding_api_key,
        "EVEROS_EMBEDDING__BASE_URL": settings.embedding_base_url,
        "EVEROS_EMBEDDING__MODEL": settings.embedding_model,
        "EVEROS_LLM__API_KEY": settings.llm_api_key,
        "EVEROS_LLM__BASE_URL": settings.llm_base_url,
        "EVEROS_LLM__MODEL": settings.llm_model,
        "EVEROS_ROOT": str(everos_root),
        "HOME": str(child_home),
        "MEMORY_POC_OWNER_ID": owner_id,
        "MEMORY_POC_REQUEST_METRICS": str(metrics_path),
        "PATH": f"{python.parent}:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
    }
    for key in PROXY_AND_TLS_ENV_KEYS:
        env.pop(key, None)
    return env


def checked_workspace_root(root: Path | None = None) -> Path:
    checkout = Path(os.path.abspath(root or workspace_root()))
    info = checkout.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HarnessError("workspace_root_unsafe")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise HarnessError("workspace_root_owner_mismatch")
    return checkout
