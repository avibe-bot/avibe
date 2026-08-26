"""Integrity checks shared by local installers and the runtime doctor.

Python installers record the files they put on disk in ``RECORD``.  A package
version is not proof that those files are still complete: an interrupted copy
can leave valid metadata beside a partial package tree.  These helpers make
the file contents the source of truth without importing the package being
checked.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


RUNTIME_DISTRIBUTIONS = ("avibe-os", "vibe-remote")
RUNTIME_PROBE_PREFIX = "avibe-runtime-distributions:"


@dataclass(frozen=True)
class IntegrityResult:
    """Measured result of a package-tree verification."""

    ok: bool
    checked_files: int = 0
    failures: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        if self.ok:
            return f"verified {self.checked_files} package files"
        if not self.failures:
            return "package integrity verification failed"
        suffix = ", ".join(self.failures[:5])
        if len(self.failures) > 5:
            suffix += f", and {len(self.failures) - 5} more"
        return suffix


@dataclass(frozen=True)
class DependencyGraph:
    """Installed distributions reachable from Avibe's wheel metadata."""

    distributions: tuple[str, ...] = ()
    records: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


def isolated_probe_environment() -> dict[str, str]:
    """Keep the candidate interpreter from importing the caller's checkout."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return environment


def run_isolated_probe(command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a candidate probe from a private empty working directory."""

    with tempfile.TemporaryDirectory(prefix="avibe-integrity-") as working_directory:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=working_directory,
            env=isolated_probe_environment(),
        )


def site_packages_for_python(python_executable: str | os.PathLike[str]) -> list[Path]:
    """Return site-packages directories belonging to a Python executable."""

    # Keep the logical path intact. uv tool interpreters are often symlinks to
    # a shared Python binary, while their import path and sysconfig point at
    # the tool environment selected through that symlink.
    executable = Path(python_executable).expanduser()
    discovered: list[Path] = []
    probe = (
        "import site, sysconfig; "
        "config = sysconfig.get_paths(); "
        "paths = [config.get('purelib'), config.get('platlib')]; "
        "paths += list(getattr(site, 'getsitepackages', lambda: [])()); "
        "user = getattr(site, 'getusersitepackages', lambda: '')(); "
        "paths += [user] if user and getattr(site, 'ENABLE_USER_SITE', True) else []; "
        "print('\\n'.join(dict.fromkeys(path for path in paths if path)))"
    )
    try:
        result = run_isolated_probe([str(executable), "-c", probe], timeout=10)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        discovered.extend(Path(line).expanduser() for line in result.stdout.splitlines() if line.strip())

    def has_record(path: Path) -> bool:
        return path.is_dir() and any(path.glob("*.dist-info/RECORD"))

    # The probed interpreter owns its import-path boundary. Filesystem globs
    # cannot infer that boundary when a prefix contains multiple Python
    # versions, so accept only interpreter-reported locations with wheel
    # metadata. A probe that cannot run is an invalid candidate, not a reason
    # to verify a sibling interpreter's packages.
    discovered = [path for path in discovered if has_record(path)]
    candidates: list[Path] = []
    seen: set[Path] = set()
    for path in discovered:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)
    return candidates


def record_paths(site_packages: Path, distribution_names: Iterable[str] | None = None) -> list[Path]:
    """Find RECORD files, optionally limited to normalized distribution names."""

    wanted = None
    if distribution_names is not None:
        wanted = {canonicalize_name(name) for name in distribution_names}
    paths: list[Path] = []
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        if wanted is not None and record_distribution_name(record) not in wanted:
            continue
        paths.append(record)
    return paths


def record_distribution_name(record: Path) -> str:
    """Return the canonical distribution name owning one wheel RECORD."""

    stem = record.parent.name.removesuffix(".dist-info")
    return canonicalize_name(stem.rsplit("-", 1)[0])


def verify_site_packages(
    site_packages: Path | str,
    *,
    distribution_names: Iterable[str] | None = None,
) -> IntegrityResult:
    """Verify every hashed file named by installed distribution RECORD files.

    The RECORD file itself is allowed to have an empty hash, as specified by
    the wheel format.  Paths that escape ``site_packages`` are rejected rather
    than resolved, so a malformed record cannot turn this check into a read of
    an unrelated file.
    """

    root = Path(site_packages).expanduser().resolve()
    records = record_paths(root, distribution_names)
    return verify_record_files(root, records)


def verify_record_files(site_packages: Path | str, records: Iterable[Path]) -> IntegrityResult:
    """Verify exact wheel RECORD files selected by the candidate interpreter."""

    root = Path(site_packages).expanduser().resolve()
    failures: list[str] = []
    safe_records: list[Path] = []
    for record in records:
        try:
            record = Path(record).expanduser().resolve()
            record.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            failures.append(f"unsafe RECORD path {record}")
            continue
        if record.name != "RECORD" or not record.parent.name.endswith(".dist-info"):
            failures.append(f"unsafe RECORD path {record}")
            continue
        safe_records.append(record)
    records = tuple(safe_records)
    if not records:
        failures.append(f"no RECORD file under {root}")
        return IntegrityResult(False, failures=tuple(failures))

    checked = 0
    for record_path in records:
        try:
            rows = list(csv.reader(record_path.read_text(encoding="utf-8").splitlines()))
        except (OSError, UnicodeError, csv.Error) as exc:
            failures.append(f"{record_path.name}: {exc}")
            continue
        for row in rows:
            if len(row) < 3 or not row[0]:
                failures.append(f"{record_path.name}: malformed RECORD row")
                continue
            relative = PurePosixPath(row[0])
            if relative.is_absolute():
                failures.append(f"{record_path.name}: unsafe path {row[0]}")
                continue
            path = (root / Path(*relative.parts)).resolve()
            # Wheel RECORD paths are relative to site-packages and legitimately
            # include ``../../../bin/<entrypoint>`` for generated scripts.  The
            # enclosing tool root is the trust boundary, not site-packages
            # itself; anything that escapes that root is still rejected.
            if root.name in {"site-packages", "dist-packages"} and root.parent.name.startswith("python"):
                allowed_root = root.parent.parent.parent.resolve()
            elif root.name in {"site-packages", "dist-packages"} and root.parent.name == "Lib":
                allowed_root = root.parent.parent.resolve()
            else:
                allowed_root = root
            try:
                path.relative_to(allowed_root)
            except ValueError:
                failures.append(f"{record_path.name}: escaped path {row[0]}")
                continue
            if not path.is_file():
                failures.append(f"missing {row[0]}")
                continue
            digest = row[1]
            expected_size = row[2]
            if digest:
                try:
                    algorithm, encoded = digest.split("=", 1)
                    if algorithm not in {"sha256", "sha384", "sha512"}:
                        failures.append(f"{row[0]}: unsupported hash {algorithm}")
                        continue
                    expected = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                    actual = hashlib.new(algorithm, path.read_bytes()).digest()
                except (OSError, ValueError, UnicodeError):
                    failures.append(f"{row[0]}: invalid hash")
                    continue
                if actual != expected:
                    failures.append(f"hash mismatch {row[0]}")
                    continue
            if expected_size:
                try:
                    if path.stat().st_size != int(expected_size):
                        failures.append(f"size mismatch {row[0]}")
                        continue
                except (OSError, ValueError):
                    failures.append(f"invalid size {row[0]}")
                    continue
            checked += 1

    return IntegrityResult(not failures, checked_files=checked, failures=tuple(failures))


def runtime_import_modules() -> tuple[str, ...]:
    """Return import boundaries for every registered user-facing platform."""

    from config.platform_registry import platform_descriptors

    return tuple(
        dict.fromkeys(
            (
                "vibe",
                *(descriptor.client_module for descriptor in platform_descriptors()),
            )
        )
    )


def inspect_dependency_graph(
    distribution_names: Iterable[str] = RUNTIME_DISTRIBUTIONS,
) -> DependencyGraph:
    """Validate the installed dependency closure rooted at Avibe's wheel metadata."""

    candidates = tuple(distribution_names)
    root = None
    root_candidate = ""
    for name in candidates:
        try:
            root = importlib_metadata.distribution(name)
            root_candidate = name
            break
        except importlib_metadata.PackageNotFoundError:
            continue
    if root is None:
        return DependencyGraph(failures=(f"missing runtime distribution: {' or '.join(candidates)}",))

    root_name = canonicalize_name(root.metadata.get("Name") or root_candidate)
    distributions = {root_name: root}
    requested_extras: dict[str, set[str]] = {root_name: set()}
    processed_extras: dict[str, frozenset[str]] = {}
    pending = deque([root_name])
    failures: set[str] = set()
    marker_environment = default_environment()

    while pending:
        distribution_name = pending.popleft()
        extras = requested_extras[distribution_name]
        extras_snapshot = frozenset(extras)
        if processed_extras.get(distribution_name) == extras_snapshot:
            continue
        processed_extras[distribution_name] = extras_snapshot
        distribution = distributions[distribution_name]

        for raw_requirement in distribution.requires or ():
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement:
                failures.add(f"invalid dependency metadata: {raw_requirement}")
                continue
            marker_extras = extras or {""}
            if requirement.marker is not None and not any(
                requirement.marker.evaluate({**marker_environment, "extra": extra})
                for extra in marker_extras
            ):
                continue

            dependency_name = canonicalize_name(requirement.name)
            try:
                dependency = importlib_metadata.distribution(requirement.name)
            except importlib_metadata.PackageNotFoundError:
                failures.add(f"missing dependency: {requirement.name}")
                continue
            if requirement.specifier and not requirement.specifier.contains(
                dependency.version,
                prereleases=True,
            ):
                failures.add(
                    f"incompatible dependency: {requirement.name} {dependency.version} "
                    f"does not satisfy {requirement.specifier}"
                )
                continue

            distributions[dependency_name] = dependency
            dependency_extras = requested_extras.setdefault(dependency_name, set())
            previous_extras = frozenset(dependency_extras)
            dependency_extras.update(requirement.extras)
            if dependency_name not in processed_extras or previous_extras != frozenset(dependency_extras):
                pending.append(dependency_name)

    records: list[str] = []
    for distribution_name, distribution in distributions.items():
        record = next(
            (
                file
                for file in (distribution.files or ())
                if file.name == "RECORD" and file.parent.name.endswith(".dist-info")
            ),
            None,
        )
        if record is None:
            failures.add(f"no RECORD for dependency {distribution_name}")
            continue
        records.append(str(distribution.locate_file(record)))

    return DependencyGraph(
        distributions=tuple(sorted(distributions)),
        records=tuple(sorted(dict.fromkeys(records))),
        failures=tuple(sorted(failures)),
    )


def probe_runtime_environment(required_imports: Iterable[str] | None = None) -> tuple[str, ...]:
    """Raise when declared dependencies or registered platform imports are broken."""

    graph = inspect_dependency_graph()
    if graph.failures:
        raise RuntimeError("; ".join(graph.failures))
    modules = runtime_import_modules() if required_imports is None else tuple(required_imports)
    for module in modules:
        if module:
            import_module(module)
    return graph.records


def verify_python_environment(
    python_executable: str | os.PathLike[str],
    *,
    required_imports: Iterable[str] | None = None,
    timeout: float = 30.0,
) -> IntegrityResult:
    """Verify package records, dependency closure, and platform imports."""

    executable = str(Path(python_executable).expanduser())
    site_packages = site_packages_for_python(executable)
    if not site_packages:
        return IntegrityResult(False, failures=(f"no site-packages for {executable}",))

    probe_call = (
        "probe_runtime_environment()"
        if required_imports is None
        else f"probe_runtime_environment({tuple(module for module in required_imports if module)!r})"
    )
    code = (
        "import json; "
        "from core.install_integrity import probe_runtime_environment; "
        f"print({RUNTIME_PROBE_PREFIX!r} + json.dumps({probe_call}))"
    )
    try:
        process = run_isolated_probe([executable, "-c", code], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return IntegrityResult(False, failures=(f"runtime probe failed: {exc}",))
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "runtime probe failed").strip().splitlines()[-1]
        return IntegrityResult(False, failures=(f"runtime probe failed: {detail}",))

    dependency_line = next(
        (line for line in process.stdout.splitlines() if line.startswith(RUNTIME_PROBE_PREFIX)),
        None,
    )
    try:
        record_names = tuple(json.loads(dependency_line.removeprefix(RUNTIME_PROBE_PREFIX)))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return IntegrityResult(False, failures=("runtime probe did not report its dependency records",))

    failures: list[str] = []
    checked = 0
    records_by_site: dict[Path, list[Path]] = {site_package: [] for site_package in site_packages}
    for record_name in record_names:
        try:
            record = Path(record_name).expanduser().resolve()
            site_package = next(root for root in site_packages if record.is_relative_to(root))
        except (OSError, RuntimeError, StopIteration):
            failures.append(f"dependency RECORD outside candidate site-packages: {record_name}")
            continue
        records_by_site[site_package].append(record)
    for site_package, records in records_by_site.items():
        if not records:
            continue
        result = verify_record_files(site_package, records)
        checked += result.checked_files
        failures.extend(result.failures)
    if failures:
        return IntegrityResult(False, checked_files=checked, failures=tuple(failures))

    return IntegrityResult(True, checked_files=checked)
