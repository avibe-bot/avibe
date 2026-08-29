from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from vibe.package_shape import (
    CORE_PACKAGE_NAME,
    LEGACY_CORE_PACKAGE_NAME,
    MEMORY_PACKAGE_NAME as SHAPE_MEMORY_PACKAGE_NAME,
)


logger = logging.getLogger(__name__)

PACKAGE_NAME = CORE_PACKAGE_NAME
LEGACY_PACKAGE_NAME = LEGACY_CORE_PACKAGE_NAME
MEMORY_PACKAGE_NAME = SHAPE_MEMORY_PACKAGE_NAME
MEMORY_EXTRA_NAME = "memory"
PIP_DOWNLOAD_DEST_PLACEHOLDER = "{avibe-pip-download-destination}"
DEFAULT_UPDATE_METADATA_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
CURRENT_VIBE_EXECUTABLE_ENV = "VIBE_CURRENT_EXECUTABLE"
SHOW_RUNTIME_SKIP_ENV = "VIBE_INSTALL_SKIP_SHOW_RUNTIME"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
UV_FALLBACK_BIN_DIRS = (".local/bin", ".cargo/bin")
# A spec that names nothing but a package, so appending ``==<version>`` to it
# yields a requirement rather than a broken string. PEP 508 names only:
# anything with a path separator, a URL scheme, extras, a marker, or a version
# of its own is deliberately excluded. See `pinned_package_spec`.
_BARE_PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# PEP 440-ish parser: release + optional pre-release (a/b/rc) + optional
# post + optional dev, plus a local version segment (``+local``) that we
# accept but ignore for ordering. Word forms (alpha/beta/preview) are listed
# before their single-letter aliases so the alternation does not match a bare
# leading letter (e.g. the "a" in "alpha").
_VERSION_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[._-]?(?P<pre>alpha|beta|preview|pre|rc|a|b|c)[._-]?(?P<pre_num>\d+)?)?"
    r"(?:[._-]?(?P<post>post)[._-]?(?P<post_num>\d+)?)?"
    r"(?:[._-]?(?P<dev>dev)[._-]?(?P<dev_num>\d+)?)?"
    r"(?:\+(?P<local>[a-z0-9._-]+))?\s*$",
    re.IGNORECASE,
)
# Relative ordering of pre-release stages: a/alpha < b/beta < c/rc/pre/preview.
_PRE_ORDER = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "rc": 2,
    "pre": 2,
    "preview": 2,
}


class MemoryRequirementUnreadableError(ValueError):
    """Persisted configuration cannot safely decide Memory package shape."""


@dataclass(frozen=True)
class UpgradePlan:
    """A validated package-install command and its execution metadata."""

    command: list[str]
    env: dict[str, str] | None
    method: str
    preflight_command: list[str] | None = None
    preflight_fallback_command: list[str] | None = None


def execute_upgrade_plan(
    plan: UpgradePlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    **run_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Run a validated plan after its non-mutating preflight.

    The caller owns package mutation sequencing. This helper deliberately does
    not acquire a lifecycle lock: callers can run the plan synchronously while
    the existing restart supervisor owns any later activation.
    """

    preflight = preflight_upgrade_plan(plan, run=run, **run_kwargs)
    if preflight is not None:
        if preflight.returncode != 0:
            return preflight

    return run(plan.command, env=plan.env, **run_kwargs)


def preflight_upgrade_plan(
    plan: UpgradePlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    **run_kwargs: object,
) -> subprocess.CompletedProcess[str] | None:
    """Resolve a plan without mutating the environment."""

    if plan.preflight_command is None:
        return None

    def run_preflight(command: list[str]) -> subprocess.CompletedProcess[str]:
        scratch_dir: str | None = None
        try:
            if PIP_DOWNLOAD_DEST_PLACEHOLDER in command:
                scratch_dir = tempfile.mkdtemp(prefix="avibe-pip-download-")
                command = [
                    scratch_dir if argument == PIP_DOWNLOAD_DEST_PLACEHOLDER else argument
                    for argument in command
                ]
            return run(command, env=plan.env, **run_kwargs)
        finally:
            if scratch_dir is not None:
                shutil.rmtree(scratch_dir, ignore_errors=True)

    preflight = run_preflight(plan.preflight_command)
    if (
        preflight.returncode == 0
        or plan.preflight_fallback_command is None
        or not _pip_dry_run_is_unsupported(preflight)
    ):
        return preflight

    return run_preflight(plan.preflight_fallback_command)


def _pip_dry_run_is_unsupported(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return "--dry-run" in output and any(
        marker in output
        for marker in ("no such option", "unknown option", "unrecognized argument")
    )


def resolve_command_path(command: str | None, search_path: str | None = None) -> str | None:
    if not command:
        return None

    expanded = Path(command).expanduser()
    if expanded.is_absolute():
        return os.path.abspath(str(expanded))

    if any(sep in command for sep in (os.sep, "/")):
        return os.path.abspath(str(Path.cwd() / expanded))

    resolved = shutil.which(command, path=search_path)
    if not resolved:
        return None
    return os.path.abspath(os.path.expanduser(resolved))


def is_usable_command_path(path: str | None) -> bool:
    if not path:
        return False
    return os.path.exists(path) and os.access(path, os.X_OK)


def get_launcher_bin_dir(command_path: str) -> str:
    current = os.path.abspath(os.path.expanduser(command_path))

    while os.path.islink(current):
        target = os.readlink(current)
        if not os.path.isabs(target):
            target = os.path.abspath(os.path.join(os.path.dirname(current), target))
        else:
            target = os.path.abspath(os.path.expanduser(target))

        if not os.path.islink(target):
            return str(Path(current).parent)

        current = target

    return str(Path(current).parent)


def get_known_uv_paths(base_env: Mapping[str, str] | None = None) -> list[str]:
    env = base_env or os.environ
    home = env.get("HOME")
    if home is not None:
        return [os.path.join(home, bin_dir, "uv") for bin_dir in UV_FALLBACK_BIN_DIRS]
    return [os.path.expanduser(f"~/{bin_dir}/uv") for bin_dir in UV_FALLBACK_BIN_DIRS]


def should_skip_show_runtime_prepare(base_env: Mapping[str, str] | None = None) -> bool:
    env = base_env or os.environ
    return env.get(SHOW_RUNTIME_SKIP_ENV, "").strip().lower() in TRUTHY_ENV_VALUES


def find_uv_binary(uv_path: str | None = None, base_env: Mapping[str, str] | None = None) -> str | None:
    env = base_env or os.environ
    search_path = env.get("PATH")

    resolved = resolve_command_path(uv_path, search_path=search_path)
    if is_usable_command_path(resolved):
        return resolved

    resolved = resolve_command_path("uv", search_path=search_path)
    if is_usable_command_path(resolved):
        return resolved

    for candidate in get_known_uv_paths(base_env=env):
        resolved = resolve_command_path(candidate, search_path=search_path)
        if is_usable_command_path(resolved):
            return resolved

    return None


def get_running_vibe_path(
    *,
    vibe_path: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> str | None:
    resolved = resolve_command_path(vibe_path, search_path=search_path)
    if is_usable_command_path(resolved):
        return resolved

    env_path = resolve_command_path(os.environ.get(CURRENT_VIBE_EXECUTABLE_ENV), search_path=search_path)
    if is_usable_command_path(env_path):
        return env_path

    argv_path = resolve_command_path(argv0 or sys.argv[0], search_path=search_path)
    if is_usable_command_path(argv_path):
        argv_path_str = cast(str, argv_path)
        if Path(argv_path_str).name.startswith("vibe"):
            return argv_path_str

    fallback_path = resolve_command_path("vibe", search_path=search_path)
    if is_usable_command_path(fallback_path):
        return fallback_path
    return None


def cache_running_vibe_path(vibe_path: str | None = None) -> str | None:
    resolved = get_running_vibe_path(vibe_path=vibe_path)
    if resolved:
        os.environ[CURRENT_VIBE_EXECUTABLE_ENV] = resolved
    return resolved


def get_restart_command(
    *,
    vibe_path: str | None = None,
    python_executable: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> list[str]:
    resolved = get_running_vibe_path(vibe_path=vibe_path, argv0=argv0, search_path=search_path)
    if resolved:
        return [resolved]
    return [python_executable or sys.executable, "-c", "from vibe.cli import main; main()"]


def get_restart_invocation_command(
    *,
    vibe_path: str | None = None,
    python_executable: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> list[str]:
    return [
        *get_restart_command(
            vibe_path=vibe_path,
            python_executable=python_executable,
            argv0=argv0,
            search_path=search_path,
        ),
        "restart",
    ]


def _get_source_checkout_root() -> str | None:
    source_root = Path(__file__).resolve().parent.parent
    if not source_root.is_dir():
        return None
    if not (source_root / "pyproject.toml").is_file():
        return None
    if not (source_root / "vibe" / "__init__.py").is_file():
        return None
    return str(source_root)


def _normalize_pythonpath_entries(pythonpath: str) -> list[str]:
    normalized_entries: list[str] = []
    seen_entries: set[str] = set()

    for entry in pythonpath.split(os.pathsep):
        if not entry:
            continue
        normalized_entry = os.path.abspath(os.path.expanduser(entry))
        if normalized_entry in seen_entries:
            continue
        seen_entries.add(normalized_entry)
        normalized_entries.append(normalized_entry)

    return normalized_entries


def get_restart_environment(
    *,
    vibe_path: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    resolved = get_running_vibe_path(vibe_path=vibe_path, argv0=argv0, search_path=search_path)
    if resolved:
        return None

    source_root = _get_source_checkout_root()
    if not source_root:
        return None

    env = dict(base_env or os.environ)
    pythonpath = env.get("PYTHONPATH")
    if pythonpath:
        normalized_root = os.path.abspath(source_root)
        normalized_entries = _normalize_pythonpath_entries(pythonpath)
        if normalized_root not in normalized_entries:
            normalized_entries.insert(0, normalized_root)
        env["PYTHONPATH"] = os.pathsep.join(normalized_entries)
        return env

    env["PYTHONPATH"] = source_root
    return env


def get_restart_shell_command(
    *,
    vibe_path: str | None = None,
    python_executable: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> str:
    command = get_restart_invocation_command(
        vibe_path=vibe_path,
        python_executable=python_executable,
        argv0=argv0,
        search_path=search_path,
    )
    return shlex.join(command)


def get_update_metadata_url() -> str:
    return os.environ.get("AVIBE_UPDATE_METADATA_URL") or os.environ.get(
        "VIBE_UPDATE_METADATA_URL", DEFAULT_UPDATE_METADATA_URL
    )


def get_upgrade_package_spec() -> str:
    return os.environ.get("AVIBE_UPGRADE_PACKAGE_SPEC") or os.environ.get("VIBE_UPGRADE_PACKAGE_SPEC", PACKAGE_NAME)


def _normalize_release_parts(parts: tuple[int, ...]) -> tuple[int, ...]:
    normalized = list(parts)
    while len(normalized) > 1 and normalized[-1] == 0:
        normalized.pop()
    return tuple(normalized)


def _parse_version_parts(
    value: str,
) -> tuple[tuple[int, ...], tuple[int, int] | None, int | None, int | None] | None:
    """Split a version into (release, pre, post, dev).

    ``pre`` is ``(stage_order, num)`` for a/b/rc releases, else ``None``.
    ``post`` / ``dev`` are the numeric component, or ``None`` when absent.
    The local segment (``+...``) is matched but intentionally discarded: per
    PEP 440 it does not affect ordering.
    """
    match = _VERSION_RE.match(value)
    if not match:
        return None

    release = _normalize_release_parts(
        tuple(int(part) for part in match.group("release").split("."))
    )
    pre = None
    if match.group("pre"):
        pre = (_PRE_ORDER[match.group("pre").lower()], int(match.group("pre_num") or "0"))
    post = int(match.group("post_num") or "0") if match.group("post") else None
    dev = int(match.group("dev_num") or "0") if match.group("dev") else None
    return (release, pre, post, dev)


def _version_key(
    parts: tuple[tuple[int, ...], tuple[int, int] | None, int | None, int | None],
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Build a totally-ordered, all-int comparison key (PEP 440 ordering).

    Bands are plain int tuples so they never compare a number against a tuple.
    Within a release: ``X.devN`` < ``Xa/b/rcN`` < ``X`` (final) < ``X.postN``,
    and a trailing ``.devN`` sorts before the same version without it.
    """
    release, pre, post, dev = parts
    if pre is None and post is None and dev is not None:
        pre_band: tuple[int, ...] = (0,)  # bare X.devN sorts before any pre/final of X
    elif pre is None:
        pre_band = (2,)  # final (or post-only) sorts after pre-releases
    else:
        pre_band = (1, pre[0], pre[1])
    post_band = (0,) if post is None else (1, post)
    dev_band = (1,) if dev is None else (0, dev)  # a dev release sorts before the non-dev
    return (release, pre_band, post_band, dev_band)


def _parse_version(
    value: str,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]] | None:
    parts = _parse_version_parts(value)
    return None if parts is None else _version_key(parts)


def _is_prerelease_version(value: str) -> bool:
    parts = _parse_version_parts(value)
    if parts is None:
        return False
    _release, pre, _post, dev = parts
    return pre is not None or dev is not None


def _is_yanked_release(files: object) -> bool:
    if not isinstance(files, list) or not files:
        return False
    yanked_flags = [bool(item.get("yanked")) for item in files if isinstance(item, dict)]
    return bool(yanked_flags) and all(yanked_flags)


def select_latest_update_version(metadata: Mapping[str, object], current_version: str) -> str:
    allow_prereleases = _is_prerelease_version(current_version)
    releases = metadata.get("releases")

    candidates: list[tuple[object, str]] = []
    if isinstance(releases, Mapping):
        for version_str, files in releases.items():
            if not isinstance(version_str, str):
                continue
            parsed = _parse_version(version_str)
            if parsed is None:
                continue
            if not allow_prereleases and _is_prerelease_version(version_str):
                continue
            if _is_yanked_release(files):
                continue
            candidates.append((parsed, version_str))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    latest = str((metadata.get("info") or {}).get("version") or "")
    if latest and (allow_prereleases or not _is_prerelease_version(latest)):
        return latest
    return ""


def has_newer_version(candidate: str, current: str) -> bool:
    if not candidate or candidate == current:
        return False

    latest_parsed = _parse_version(candidate)
    current_parsed = _parse_version(current)
    if latest_parsed is not None and current_parsed is not None:
        return latest_parsed > current_parsed

    # Fallback for strings the parser cannot handle: compare the leading
    # integer of each of the first three dotted components. Stop at the first
    # component without a leading digit so we never silently drop a position
    # (e.g. treating "3.0.4rc4" as [3, 0] and ranking it below "3.0.3").
    def _loose_parts(text: str) -> list[int]:
        parts: list[int] = []
        for chunk in text.split(".")[:3]:
            digits = re.match(r"\d+", chunk)
            if not digits:
                break
            parts.append(int(digits.group()))
        return parts

    try:
        return _loose_parts(candidate) > _loose_parts(current)
    except (ValueError, AttributeError):
        return candidate != current


def get_latest_version_info(current_version: str) -> dict:
    result = {"current": current_version, "latest": None, "has_update": False, "error": None}

    try:
        url = get_update_metadata_url()
        req = urllib.request.Request(url, headers={"User-Agent": PACKAGE_NAME})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        latest = select_latest_update_version(data, current_version)
        result["latest"] = latest

        if latest and latest != current_version:
            result["has_update"] = has_newer_version(latest, current_version)
    except Exception as e:
        result["error"] = str(e)

    return result


def is_uv_tool_install(python_executable: str | None = None) -> bool:
    executable = (python_executable or sys.executable or "").replace("\\", "/")
    return "/uv/tools/" in executable


def is_legacy_uv_tool_install(python_executable: str | None = None) -> bool:
    executable = (python_executable or sys.executable or "").replace("\\", "/")
    return f"/uv/tools/{LEGACY_PACKAGE_NAME}/" in executable


def memory_package_installed() -> bool:
    """Return whether the optional Memory distribution is installed."""

    try:
        from importlib.metadata import PackageNotFoundError, distribution

        distribution(MEMORY_PACKAGE_NAME)
    except PackageNotFoundError:
        return False
    except Exception:
        logger.warning("Could not inspect %s metadata; preserving package shape", MEMORY_PACKAGE_NAME, exc_info=True)
        return True
    return True


def configured_memory_enabled() -> bool:
    """Read the persisted Memory switch for an explicit upgrade action."""

    try:
        from config.v2_config import V2Config

        required = V2Config.load().memory_required
    except FileNotFoundError:
        return False
    except Exception as exc:
        raise MemoryRequirementUnreadableError(
            "The persisted Memory requirement could not be read"
        ) from exc
    if required is None:
        raise MemoryRequirementUnreadableError(
            "The persisted Memory requirement could not be read"
        )
    return required


def _distributions_providing_this_package() -> list[str]:
    """Every installed distribution that provides the package this module is in."""

    try:
        from importlib.metadata import packages_distributions
        from packaging.utils import canonicalize_name
    except ImportError:  # pragma: no cover - importlib.metadata ships with 3.10+
        return []
    try:
        memory_name = canonicalize_name(MEMORY_PACKAGE_NAME)
        return sorted(
            {
                name
                for name in packages_distributions().get(__name__.split(".")[0], [])
                if canonicalize_name(name) != memory_name
            }
        )
    except Exception:  # pragma: no cover - a broken environment answers nothing
        logger.debug("Failed to read installed distribution metadata", exc_info=True)
        return []


def _providers_recording_a_published_release() -> list[tuple[str, str]]:
    """Every distribution providing this package, with the release it records.

    Unpublished recordings are dropped rather than reported. A `dev` or local
    version records the tree it was built from, so it can neither confirm nor
    contradict a released one, and an environment that answers nothing at all --
    no provider, unreadable metadata, nothing published -- comes back empty. Both
    callers read empty as "no evidence", never as evidence of disagreement.
    """

    distributions = _distributions_providing_this_package()
    if not distributions:
        return []
    try:
        from importlib.metadata import version as distribution_version

        recorded = [(name, distribution_version(name)) for name in distributions]
    except Exception:  # pragma: no cover - a broken environment answers nothing
        logger.debug("Failed to read the installed distribution version", exc_info=True)
        return []
    return [(name, version) for name, version in recorded if _names_a_published_release(version)]


def _providers_describing_running_code() -> list[str]:
    """Return providers whose recorded release matches the running code.

    Multiple providers can remain after the `vibe-remote` to `avibe-os` rename.
    Comparing every published record prevents a forward install from becoming a
    silent no-op when stale metadata claims the requested release is present.
    """

    from vibe import __version__

    return [
        name
        for name, recorded in _providers_recording_a_published_release()
        if recorded == __version__
    ]


def installed_metadata_describes_running_code() -> bool:
    """Whether every published provider record matches the running files.

    pip trusts installed metadata when deciding whether an upgrade has work to
    do. A mismatch therefore forces the forward reinstall. Unknown source or
    editable layouts remain unforced because they provide no reliable published
    version to compare.
    """

    from vibe import __version__

    if not _names_a_published_release(__version__):
        return True

    published = _providers_recording_a_published_release()
    if not published:
        return True
    # Metadata describes the code only if EVERY provider recording a published
    # release records this one; a single dissenter is a distribution pip would
    # read as already satisfied.
    return len(_providers_describing_running_code()) == len(published)


def get_current_vibe_bin_dir(vibe_path: str | None = None) -> str | None:
    current_vibe = get_running_vibe_path(vibe_path=vibe_path)
    if not current_vibe:
        return None

    return get_launcher_bin_dir(current_vibe)


def _names_a_published_release(version: str) -> bool:
    """Whether an index could serve `version`, as opposed to it naming this tree.

    A development build's version describes the tree it was built from rather
    than anything published: PEP 440 spells that as a `dev` segment, a local
    segment (`+<sha>`), or both, and the regression builder emits exactly that
    shape.

    Asking the property instead of listing the strings is what stops the next
    unlisted shape from costing the same attempt. Unparseable reads as
    unpublished for the same reason: a version this codebase cannot even order is
    not one it should pin an install to.
    """

    match = _VERSION_RE.match(version)
    if match is None:
        return False
    return not (match.group("dev") or match.group("local"))


def _with_memory_extra(package_spec: str) -> str:
    """Add the Memory extra without corrupting URL or local path specs."""

    if package_spec.startswith(("git+", "hg+", "svn+", "bz+", "http://", "https://", "file://")):
        return f"{PACKAGE_NAME}[{MEMORY_EXTRA_NAME}] @ {package_spec}"
    try:
        requirement = Requirement(package_spec)
    except InvalidRequirement:
        artifact_uri = Path(package_spec).expanduser().resolve().as_uri()
        return f"{PACKAGE_NAME}[{MEMORY_EXTRA_NAME}] @ {artifact_uri}"

    extras = sorted({*requirement.extras, MEMORY_EXTRA_NAME})
    rendered = f"{requirement.name}[{','.join(extras)}]"
    if requirement.url:
        rendered += f" @ {requirement.url}"
    else:
        rendered += str(requirement.specifier)
    if requirement.marker:
        rendered += f"; {requirement.marker}"
    return rendered


def _published_version(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        version = str(Version(value))
    except InvalidVersion:
        return None
    return version if _names_a_published_release(version) else None


def _memory_target_version(package_spec: str, target_version: str | None) -> str | None:
    """Choose the Memory pin from the artifact being installed or valid metadata."""

    try:
        requirement = Requirement(package_spec)
    except InvalidRequirement:
        artifact = package_spec
    else:
        if requirement.url is None:
            specifiers = list(requirement.specifier)
            if (
                len(specifiers) == 1
                and specifiers[0].operator == "=="
                and "*" not in specifiers[0].version
            ):
                return _published_version(specifiers[0].version)
            candidate = _published_version(target_version)
            if candidate is not None and requirement.specifier.contains(candidate, prereleases=True):
                return candidate
            return None
        artifact = requirement.url

    if artifact.startswith(("git+", "hg+", "svn+", "bz+")):
        return None
    try:
        parsed = urllib.parse.urlsplit(artifact)
    except ValueError:
        return None
    filename = Path(urllib.parse.unquote(parsed.path if parsed.scheme else artifact)).name
    try:
        _, version, _, _ = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        try:
            _, version = parse_sdist_filename(filename)
        except InvalidSdistFilename:
            return None
    return _published_version(str(version))


def pinned_package_spec(
    version: str,
    *,
    package_name: str,
) -> str:
    """Pin one explicit distribution name to one exact published version.

    The explicit Memory install uses this to reinstall the running core release
    together with its matching optional package. The distribution name must be a
    bare package name; direct URLs and pre-versioned requirements are rejected.

    The message never quotes the spec. An operator-supplied spec can be an index
    URL carrying credentials.
    """

    spec = package_name
    if _BARE_PACKAGE_NAME_RE.fullmatch(spec) is None:
        raise ValueError("The configured upgrade package spec cannot carry a version pin")
    return f"{spec}=={version}"


def build_upgrade_plan(
    *,
    python_executable: str | None = None,
    uv_path: str | None = None,
    vibe_path: str | None = None,
    base_env: dict[str, str] | None = None,
    version: str | None = None,
    target_version: str | None = None,
    package_name: str | None = None,
    memory_enabled: bool = False,
    memory_package: bool | None = None,
    memory_version: str | None = None,
    package_spec: str | None = None,
) -> UpgradePlan:
    """How to install avibe: the newest release, or `version` exactly.

    Exact-version plans support the explicit Memory package install action. They
    replace the ordinary upgrade request and force the installer so the matching
    optional distribution is applied even when core is already satisfied.
    """

    executable = python_executable or sys.executable
    # A caller targeting another interpreter (for example a test-owned venv)
    # can provide an explicit package-shape measurement.  Only infer from this
    # process when the caller did not provide one; otherwise ambient metadata
    # would leak into the target plan and turn an intentional core-only install
    # into a Memory install.
    include_memory = (
        bool(memory_package)
        if version or memory_package is not None
        else bool(memory_enabled or memory_package_installed())
    )
    package_spec = (
        pinned_package_spec(
            version,
            package_name=package_name or PACKAGE_NAME,
        )
        if version
        else (package_spec or get_upgrade_package_spec())
    )
    if not version and include_memory:
        target_version = _memory_target_version(package_spec, target_version)
        if target_version is None:
            raise ValueError("A Memory-preserving upgrade requires a target release version")
    uv_binary = find_uv_binary(uv_path=uv_path, base_env=base_env)
    if not version and include_memory and f"[{MEMORY_EXTRA_NAME}]" not in package_spec:
        package_spec = _with_memory_extra(package_spec)
    memory_target = memory_version if version else target_version
    pinned_memory_spec = f"{MEMORY_PACKAGE_NAME}=={memory_target}" if include_memory and memory_target else None

    if is_uv_tool_install(executable) and uv_binary:
        env = dict(base_env or os.environ)
        vibe_bin_dir = get_current_vibe_bin_dir(vibe_path)
        if vibe_bin_dir:
            env["UV_TOOL_BIN_DIR"] = vibe_bin_dir
        command = [uv_binary, "tool", "install", package_spec]
        if pinned_memory_spec:
            command.extend(["--with", pinned_memory_spec])
        if not version:
            command.append("--upgrade")
        if version or package_spec != PACKAGE_NAME or is_legacy_uv_tool_install(executable):
            command.append("--force")
        preflight_command = None
        if include_memory:
            preflight_command = [uv_binary, "pip", "install", "--dry-run", "--python", executable]
            if not version:
                preflight_command.append("--upgrade")
            preflight_command.append(package_spec)
            if pinned_memory_spec:
                preflight_command.append(pinned_memory_spec)
        return UpgradePlan(
            command=command,
            env=env,
            method="uv",
            preflight_command=preflight_command,
        )

    command = [executable, "-m", "pip", "install"]
    if not version:
        command.append("--upgrade")
    # pip decides whether to act from metadata. Exact installs always force so a
    # matching optional package is applied even when core is already satisfied;
    # forward installs force only when published provider metadata disagrees with
    # the running files.
    if version or not installed_metadata_describes_running_code():
        command.append("--force-reinstall")
    command.append(package_spec)
    if pinned_memory_spec:
        command.append(pinned_memory_spec)
    preflight_command = None
    # Preflight only when the optional package shape is part of the operation.
    # Core-only forward installs retain the origin/dev synchronous behavior: the
    # service is still running while pip resolves, so a second resolver pass is
    # unnecessary general-updater machinery.
    preflight_fallback_command = None
    if include_memory and not version:
        preflight_command = [
            executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--upgrade",
            package_spec,
        ]
        preflight_fallback_command = [
            executable,
            "-m",
            "pip",
            "download",
            "--dest",
            PIP_DOWNLOAD_DEST_PLACEHOLDER,
            package_spec,
        ]
        if pinned_memory_spec:
            preflight_command.append(pinned_memory_spec)
            preflight_fallback_command.append(pinned_memory_spec)
    elif include_memory:
        preflight_command = [
            executable,
            "-m",
            "pip",
            "download",
            "--dest",
            PIP_DOWNLOAD_DEST_PLACEHOLDER,
        ]
        preflight_command.extend(["--no-deps", package_spec])
        if pinned_memory_spec:
            preflight_command.append(pinned_memory_spec)
    return UpgradePlan(
        command=command,
        env=dict(base_env or os.environ),
        method="pip",
        preflight_command=preflight_command,
        preflight_fallback_command=preflight_fallback_command,
    )

def get_safe_cwd() -> str:
    """Return a stable, existing absolute directory for subprocess cwd.

    The vibe service process cwd may be inside the uv tool venv directory,
    which uv deletes and recreates during upgrade.  Using the home directory
    avoids 'Current directory does not exist' errors.  Falls back to the
    system temp directory or ``/`` when HOME is unset or invalid.
    """
    for candidate in (os.path.expanduser("~"), tempfile.gettempdir(), "/"):
        if os.path.isabs(candidate) and os.path.isdir(candidate):
            return candidate
    return "/"
