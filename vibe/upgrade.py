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
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast

from vibe import runtime as runtime_mod


logger = logging.getLogger(__name__)

PACKAGE_NAME = "avibe-os"
LEGACY_PACKAGE_NAME = "vibe-remote"
MEMORY_PACKAGE_NAME = "avibe-memory"
MEMORY_EXTRA_NAME = "memory"
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
# uv names a tool's directory after the distribution it installed, so this reads
# back the name THIS process was installed under. See `installed_package_name`.
_UV_TOOL_PATH_RE = re.compile(r"/uv/tools/(?P<name>[^/]+)/")
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


@dataclass(frozen=True)
class UpgradePlan:
    """A command that installs a version of avibe, and what it will replace.

    `rollback_to` travels with the command because of WHEN it has to be taken,
    not because the two are related ideas. It describes the install this command
    is about to overwrite, and running the command is what destroys the evidence
    it is read from -- so the only safe moment to take it is before there is a
    command to run.

    Left as a function the caller was told to call early, both call sites called
    it late: after `subprocess.run(plan.command)` had already installed
    `avibe-os` over a `vibe-remote` machine, so the question "what was this
    install published as" had two answers and returned neither, and the rollback
    pinned `avibe-os==2.x`, a release that was never published under that name.
    Writing the ordering rule into a docstring is what failed; a field cannot be
    called late, because there is no plan without the measurement and no install
    without the plan.
    """

    command: list[str]
    env: dict[str, str] | None
    method: str
    rollback_to: RollbackTarget | None = None
    preflight_command: list[str] | None = None
    cleanup_command: list[str] | None = None


def execute_upgrade_plan(
    plan: UpgradePlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    **run_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Resolve, install, and normalize one package shape in that order.

    The optional preflight exists for plans that require the Memory extra. Its
    installer performs dependency resolution without writing the environment,
    so an unavailable or incompatible ``avibe-memory`` release returns before
    the command that replaces the running Avibe install. A core-only pip
    rollback needs the final cleanup because pip installs are additive: pinning
    only ``avibe-os`` does not remove an optional distribution left by the
    failed generation.
    """

    if plan.preflight_command is not None:
        preflight = run(plan.preflight_command, env=plan.env, **run_kwargs)
        if preflight.returncode != 0:
            return preflight

    installed = run(plan.command, env=plan.env, **run_kwargs)
    if installed.returncode != 0 or plan.cleanup_command is None:
        return installed

    cleaned = run(plan.cleanup_command, env=plan.env, **run_kwargs)
    return installed if cleaned.returncode == 0 else cleaned


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


def installed_package_name(python_executable: str | None = None) -> str | None:
    """The distribution this install was published as.

    Which distribution published the running version is not the same question as
    which one the next install should ask for, and the two genuinely differ: a
    machine still on `vibe-remote` upgrades to `avibe-os`, that being the rename.
    Anything going FORWARD wants the configured spec. Anything going BACK wants
    this, because `avibe-os==2.x` names a release that was never published under
    that name and an install of it can only fail.

    Asked of the installed metadata first, because that is where the answer
    actually is: the distribution that provides the running `vibe` package says
    so itself, whatever layout it was installed into. Reading it off the
    interpreter path recognised exactly one layout -- uv's tool directory -- and
    answered `None` for every supported install that is not one, pip into a
    virtualenv among them. `None` there is not neutral: the caller falls back to
    the configured FORWARD package, so a `vibe-remote` install in a virtualenv
    armed a rollback to `avibe-os==2.x` and spent the one recovery attempt on a
    release that was never published.

    The path heuristic stays as the fallback for the case metadata genuinely
    cannot answer -- a source checkout, or providers that record the same release,
    where naming one would be a guess. `None` when neither can say, which is the
    honest answer for a tree that was never installed at all.
    """

    executable = (python_executable or sys.executable or "").replace("\\", "/")
    # Only this process can be asked about its own metadata. A path belonging to
    # some OTHER interpreter is answerable only by the layout it is written in.
    if not python_executable or executable == (sys.executable or "").replace("\\", "/"):
        distributions = _distributions_providing_this_package()
        if len(distributions) == 1:
            return distributions[0]
        # More than one provider is not an unanswerable environment. It is the
        # state a rollback leaves behind and never cleans up, so it is the state
        # every subsequent upgrade measures its own rollback target in -- see
        # `_providers_describing_running_code`.
        describing = _providers_describing_running_code()
        if len(describing) == 1:
            return describing[0]

    match = _UV_TOOL_PATH_RE.search(executable)
    return match.group("name") if match else None


def memory_package_installed() -> bool:
    """Whether the optional Memory distribution is part of this install.

    Distribution metadata, rather than an implementation import, is the package
    shape shipped by the installer. An unreadable metadata environment is
    treated as present: preserving an optional package unnecessarily may stop an
    upgrade at resolution, while guessing it absent can silently remove enabled
    Memory and erase the only evidence a rollback needed.
    """

    try:
        from importlib.metadata import PackageNotFoundError, distribution

        distribution(MEMORY_PACKAGE_NAME)
    except PackageNotFoundError:
        return False
    except Exception:
        logger.warning(
            "Could not determine whether %s is installed; preserving the Memory package shape",
            MEMORY_PACKAGE_NAME,
            exc_info=True,
        )
        return True
    return True


def configured_memory_enabled() -> bool:
    """Read the persisted Memory switch for an explicit upgrade action."""

    try:
        from config.v2_config import V2Config

        return bool(V2Config.load().memory.enabled)
    except FileNotFoundError:
        return False
    except Exception:
        logger.warning(
            "Could not read Memory enablement while planning the upgrade; "
            "preserving the optional package shape",
            exc_info=True,
        )
        return True


def _distributions_providing_this_package() -> list[str]:
    """Every installed distribution that provides the package this module is in."""

    try:
        from importlib.metadata import packages_distributions
    except ImportError:  # pragma: no cover - importlib.metadata ships with 3.10+
        return []
    try:
        return sorted(set(packages_distributions().get(__name__.split(".")[0], [])))
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
    """The providers whose recorded release is the one this process is running.

    One owner for a question two callers ask for different purposes:
    `installed_package_name` wants WHICH provider describes the files on disk,
    and `installed_metadata_describes_running_code` wants WHETHER they all do.
    Answering them separately is how the rename pair got two different rulings one
    function apart -- the second was taught that two providers is the state it
    exists for, while the first went on reading it as unanswerable.

    That asymmetry is not academic, because the pair is permanent: installing
    `avibe-os` over a `vibe-remote` machine never uninstalls the older
    distribution, so once a rollback has crossed the rename the tree holds both
    forever. `avibe-os` records the release that failed and `vibe-remote` records
    the files actually running, and the metadata is what distinguishes them --
    naming neither meant every later upgrade armed its rollback with no
    distribution, and `pinned_package_spec` fell back to the configured forward
    name. `avibe-os==2.9.0` was never published, so that one recovery attempt
    could only fail, on a machine that is already down.
    """

    from vibe import __version__

    return [
        name
        for name, recorded in _providers_recording_a_published_release()
        if recorded == __version__
    ]


def installed_metadata_describes_running_code() -> bool:
    """Whether the installed distribution's recorded version matches the files.

    pip decides whether to act by reading metadata, and a rollback is the one
    operation that makes metadata stop describing the code. Installing `avibe-os`
    over a `vibe-remote` machine never uninstalls the older distribution, so
    after the rollback reinstalls `vibe-remote==3.0.10` the tree holds two: the
    files under `vibe/` are 3.0.10, and `avibe-os` still records 3.0.11 with
    nothing on disk to back it.

    The next forward upgrade is then a silent no-op. `vibe upgrade` compares this
    process's 3.0.10 against the published 3.0.11 and decides to install; pip
    reads `avibe-os==3.0.11` as already satisfied and does nothing; the command
    reports success and the machine keeps running 3.0.10. Same shape as the
    rollback no-op, one release later and with a person watching it happen.

    Measured by comparing our own two answers rather than by trusting either --
    the version this process bound at import against the version the metadata
    records for each distribution providing it. Unknown answers `True`: only a
    disagreement between two published releases is evidence, so a source checkout
    or an editable install is never forced on a guess.

    Every provider is asked, not just a single one, because the rename pair is
    the state this exists for: after a rollback across `vibe-remote` ->
    `avibe-os` the tree holds BOTH, and only one of them can be describing the
    files on disk. Treating "more than one provider" as unknown would have made
    the exact state described above the one state that answers `True` -- the
    check declining to fire on its own scenario. `installed_package_name` reads
    the same measurement for the other half of that state; both go through
    `_providers_describing_running_code` so neither can be taught it alone.
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


class RollbackTarget(NamedTuple):
    """The install being replaced, described completely enough to restore it.

    Every field travels with the others because they are one measurement, and
    splitting them is exactly how this went wrong, repeatedly and in the same
    shape each time: the version was read in the process that predates the
    install and the distribution in the one that follows it, so a `vibe-remote`
    machine pinned `avibe-os==2.9.4`, a release that never existed. Then the same
    seam reappeared one step later -- the right distribution reinstalled, and the
    service started from whatever interpreter the restarting process happened to
    be running, which after a rename is the tool that replaced this one. It put
    the failed release back up and reported the rollback a success.

    So the rule is that nothing about the replaced install is looked up again
    later. Anything a rollback needs to know about it is measured here, once, in
    the only process that can still see it, and carried. There is no constructor
    that reads one field.
    """

    version: str
    package: str | None
    launcher: runtime_mod.ServiceLauncher
    memory_package: bool = False


def _names_a_published_release(version: str) -> bool:
    """Whether an index could serve `version`, as opposed to it naming this tree.

    A development build's version describes the tree it was built from rather
    than anything published: PEP 440 spells that as a `dev` segment, a local
    segment (`+<sha>`), or both, and the regression builder emits exactly that
    shape. Testing for one hard-coded sentinel string recognised the fallback
    version and nothing else, so a regression instance armed a rollback to
    `avibe-os==0.0.0.dev0+<sha>` and spent its one recovery attempt asking an
    index for a release that cannot exist there.

    Asking the property instead of listing the strings is what stops the next
    unlisted shape from costing the same attempt. Unparseable reads as
    unpublished for the same reason: a version this codebase cannot even order is
    not one it should pin an install to.
    """

    match = _VERSION_RE.match(version)
    if match is None:
        return False
    return not (match.group("dev") or match.group("local"))


def rollback_target() -> RollbackTarget | None:
    """The install a failed restart of THIS process could be put back on.

    Everything here is measured from the running process, and that is the whole
    point rather than an implementation detail: this describes the install the
    upgrade is about to replace, and once it has been replaced there is nothing
    left on the machine to measure. Calling this from the detached restart job
    would answer for the install that did the replacing, which is not a rollback.

    Which is why the only caller is :func:`build_upgrade_plan`, and why the
    answer reaches everyone else as `UpgradePlan.rollback_to`. As a function
    anyone could reach, it was reached at the wrong time -- both upgrade paths
    called it after the install they were describing had already been overwritten.

    So: the version from this process's already-bound `__version__`, which the
    install on disk cannot change underneath it; the distribution from this
    install's own metadata; and the launcher that started this process, because
    reinstalling a distribution does not tell anyone how to run it and a rollback
    that crosses the `vibe-remote` -> `avibe-os` rename lands in a different tool
    directory than the one this process is running out of.

    `None` when the running tree reports no release an index could serve -- a
    source checkout, an editable install, a regression build. Handing that string
    on as a target would spend the one recovery attempt on a round-trip that
    fails, and then report a broken rollback mechanism to whoever is looking at a
    dark instance, when the truth is that this install never had a release to go
    back to.
    """

    from vibe import __version__

    if not _names_a_published_release(__version__):
        return None
    return RollbackTarget(
        version=__version__,
        package=installed_package_name(),
        launcher=runtime_mod.current_service_launcher(),
        memory_package=memory_package_installed(),
    )


def _with_memory_extra(package_spec: str) -> str:
    """Select the host's Memory extra without rewriting non-name specs by hand."""

    try:
        from packaging.requirements import InvalidRequirement, Requirement

        requirement = Requirement(package_spec)
    except InvalidRequirement:
        # pip and uv accept extras on local wheel paths in this form. Those paths
        # are deliberately not parsed as named PEP 508 requirements.
        return f"{package_spec}[{MEMORY_EXTRA_NAME}]"

    extras = sorted({*requirement.extras, MEMORY_EXTRA_NAME})
    rendered = f"{requirement.name}[{','.join(extras)}]"
    if requirement.url:
        rendered += f" @ {requirement.url}"
    else:
        rendered += str(requirement.specifier)
    if requirement.marker:
        rendered += f"; {requirement.marker}"
    return rendered


def pinned_package_spec(
    version: str,
    *,
    python_executable: str | None = None,
    package_name: str | None = None,
    memory_package: bool = False,
) -> str:
    """The distribution this install came from, pinned to exactly one version.

    `package_name` is the name measured before the forward install ran, and it
    wins when given. Reading the name here instead would read it on the wrong side
    of the event: the upgrade replaces the tool before it schedules the restart,
    so by the time the detached supervisor asks, the install it can see is the new
    one. Only the caller that ran before the install can answer for what was
    replaced.

    Falling back to the install on disk, and to configuration after that. A pin is
    a claim that a specific release exists under a specific name, and something
    has to witness which name that was.

    Raises when the resulting name cannot carry a pin -- a local path, a direct
    URL, or a spec that already states its own version. Refusing is the whole
    point of the function existing: the caller that wants a pin is rolling back,
    and an unpinned command resolves forward to the release it is rolling back
    FROM, reinstalling the failure and reporting success. A rollback that cannot
    be pinned has to fail loudly instead.

    The message never quotes the spec. An operator-supplied spec can be an index
    URL carrying credentials, and this error is written to a restart log.
    """

    spec = package_name or installed_package_name(python_executable) or get_upgrade_package_spec()
    if _BARE_PACKAGE_NAME_RE.fullmatch(spec) is None:
        raise ValueError("The configured upgrade package spec cannot carry a version pin")
    pinned = f"{spec}=={version}"
    return _with_memory_extra(pinned) if memory_package else pinned


def build_upgrade_plan(
    *,
    python_executable: str | None = None,
    uv_path: str | None = None,
    vibe_path: str | None = None,
    base_env: dict[str, str] | None = None,
    version: str | None = None,
    package_name: str | None = None,
    memory_enabled: bool = False,
    memory_package: bool | None = None,
) -> UpgradePlan:
    """How to install avibe: the newest release, or `version` exactly.

    A pinned plan is not an upgrade plan with an extra argument. It never asks
    for the newest release, because the caller pinning a version is going
    backwards from one -- and both package managers treat "upgrade" as a
    direction, not a preference: uv resolves forward past the pin's own
    requirement, and pip's `--upgrade` declines to install an older version than
    the one already there. So the pin replaces the upgrade request rather than
    qualifying it, and `--force` becomes unconditional on the uv path, where
    installing over an existing tool is otherwise refused.
    """

    executable = python_executable or sys.executable
    # Taken here, before the command below exists, because that command is what
    # makes it unanswerable. A pinned plan is itself a rollback and has none of
    # its own: the process building one is the release that failed, so measuring
    # there would carry the failure forward as its own recovery target.
    rollback_to = None if version else rollback_target()
    include_memory = (
        bool(memory_package)
        if version
        else bool(memory_enabled or memory_package_installed())
    )
    uv_binary = find_uv_binary(uv_path=uv_path, base_env=base_env)
    package_spec = (
        pinned_package_spec(
            version,
            python_executable=executable,
            package_name=package_name,
            memory_package=include_memory,
        )
        if version
        else get_upgrade_package_spec()
    )
    if not version and include_memory:
        package_spec = _with_memory_extra(package_spec)

    if is_uv_tool_install(executable) and uv_binary:
        env = dict(base_env or os.environ)
        vibe_bin_dir = get_current_vibe_bin_dir(vibe_path)
        if vibe_bin_dir:
            env["UV_TOOL_BIN_DIR"] = vibe_bin_dir
        command = [uv_binary, "tool", "install", package_spec]
        if not version:
            command.append("--upgrade")
        if version or package_spec != PACKAGE_NAME or is_legacy_uv_tool_install(executable):
            command.append("--force")
        preflight_command = None
        if include_memory:
            preflight_command = [
                uv_binary,
                "pip",
                "install",
                "--dry-run",
                "--python",
                executable,
            ]
            if not version:
                preflight_command.append("--upgrade")
            preflight_command.append(package_spec)
        return UpgradePlan(
            command=command,
            env=env,
            method="uv",
            rollback_to=rollback_to,
            preflight_command=preflight_command,
        )

    command = [executable, "-m", "pip", "install"]
    if not version:
        command.append("--upgrade")
    # Neither a pin nor `--upgrade` makes pip act; metadata does, and a rollback
    # is what makes metadata stop describing the code. So the command forces
    # whenever the version being asked for is not already the version on disk --
    # measured here, never read off the metadata that is the thing in doubt.
    #
    # A pinned plan always forces: it IS the rollback, running on a machine where
    # `avibe-os` was just installed over `vibe-remote`, so `pip install
    # vibe-remote==<old>` reads as already satisfied, pip does nothing, and the
    # supervisor starts the failed generation again and reports success. The uv
    # branch above has always forced a pinned install; this branch quietly did
    # not. A forward plan forces only once that has happened -- the leftover
    # `avibe-os==3.0.11` metadata would otherwise make the next upgrade a no-op
    # too, silently, with a person watching it report success.
    if version or not installed_metadata_describes_running_code():
        command.append("--force-reinstall")
    command.append(package_spec)
    preflight_command = None
    if include_memory:
        preflight_command = [
            executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
        ]
        if not version:
            preflight_command.append("--upgrade")
        preflight_command.append(package_spec)
    cleanup_command = None
    if version and not include_memory and memory_package_installed():
        cleanup_command = [
            executable,
            "-m",
            "pip",
            "uninstall",
            "--yes",
            MEMORY_PACKAGE_NAME,
        ]
    return UpgradePlan(
        command=command,
        env=dict(base_env or os.environ),
        method="pip",
        rollback_to=rollback_to,
        preflight_command=preflight_command,
        cleanup_command=cleanup_command,
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
