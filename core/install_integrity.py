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
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


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


def site_packages_for_python(python_executable: str | os.PathLike[str]) -> list[Path]:
    """Return site-packages directories belonging to a Python executable."""

    # Keep the logical path intact. uv tool interpreters are often symlinks to
    # a shared Python binary, while their import path and sysconfig point at
    # the tool environment selected through that symlink.
    executable = Path(python_executable).expanduser()
    root = executable.parent.parent
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
        result = subprocess.run(
            [str(executable), "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=tempfile.gettempdir(),
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        discovered.extend(Path(line).expanduser() for line in result.stdout.splitlines() if line.strip())

    heuristic = [
        *sorted((root / "lib").glob("python*/site-packages")),
        *sorted((root / "lib").glob("python*/dist-packages")),
        root / "Lib" / "site-packages",
    ]
    def has_record(path: Path) -> bool:
        return path.is_dir() and any(path.glob("*.dist-info/RECORD"))

    # Prefer the environment-local layout. If it is absent (for example a
    # system Python with a pip user install), retain interpreter-reported
    # locations that actually contain wheel metadata. Some Windows Python
    # builds report their environment root as ``purelib``; accepting that path
    # would make an unrelated top-level directory fail with "no RECORD".
    local = [path for path in heuristic if path.is_dir()]
    reported = [path for path in discovered if has_record(path)]
    discovered = [*local, *reported] if local else reported
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
        wanted = {name.replace("-", "_").lower() for name in distribution_names}
    paths: list[Path] = []
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        if wanted is not None:
            stem = record.parent.name.removesuffix(".dist-info")
            package = stem.rsplit("-", 1)[0].replace("-", "_").lower()
            if package not in wanted:
                continue
        paths.append(record)
    return paths


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
    if not records:
        return IntegrityResult(False, failures=(f"no RECORD file under {root}",))

    checked = 0
    failures: list[str] = []
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


def verify_python_environment(
    python_executable: str | os.PathLike[str],
    *,
    required_imports: Iterable[str] = ("vibe", "lark_oapi", "modules.im.multi"),
    timeout: float = 30.0,
) -> IntegrityResult:
    """Verify package records and import the modules required at startup."""

    executable = str(Path(python_executable).expanduser())
    site_packages = site_packages_for_python(executable)
    if not site_packages:
        return IntegrityResult(False, failures=(f"no site-packages for {executable}",))

    failures: list[str] = []
    checked = 0
    for site_package in site_packages:
        result = verify_site_packages(site_package)
        checked += result.checked_files
        failures.extend(result.failures)
    if failures:
        return IntegrityResult(False, checked_files=checked, failures=tuple(failures))

    modules = tuple(module for module in required_imports if module)
    if modules:
        code = "; ".join(f"import {module}" for module in modules)
        try:
            process = subprocess.run(
                [executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                # Do not let a source checkout on the caller's cwd satisfy the
                # probe in place of the candidate environment being checked.
                cwd=tempfile.gettempdir(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return IntegrityResult(False, checked_files=checked, failures=(f"import probe failed: {exc}",))
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "import probe failed").strip().splitlines()[-1]
            return IntegrityResult(False, checked_files=checked, failures=(f"required import failed: {detail}",))

    return IntegrityResult(True, checked_files=checked)
