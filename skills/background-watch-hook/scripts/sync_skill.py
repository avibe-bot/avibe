#!/usr/bin/env python3
"""Install and verify the canonical background-watch-hook skill."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SKILL_NAME = "background-watch-hook"
SKILL_RELATIVE_PATH = Path("skills") / SKILL_NAME
MANIFEST_NAME = ".avibe-skill-sync.json"
REQUIRED_WAIT_PR_FLAGS = (
    "--sha",
    "--workflow",
    "--seed-state",
    "--actionable-only",
    "--ignore-author",
)
DEFAULT_TARGETS = (
    Path("~/.agents/skills") / SKILL_NAME,
    Path("~/.claude/skills") / SKILL_NAME,
    Path("~/.opencode/skills") / SKILL_NAME,
)


class SyncError(RuntimeError):
    """Raised when the canonical skill or an installation target is invalid."""


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise SyncError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _resolve_repo_root(repo_root: str | None) -> Path:
    if repo_root:
        root = Path(repo_root).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[3]
    if not (root / ".git").exists() and not (root / ".git").is_file():
        raise SyncError(f"Avibe repository root is not a git repository: {root}")
    return root


def _resolve_commit(repo_root: Path, requested: str | None) -> str:
    if requested:
        return _run_git(repo_root, "rev-parse", "--verify", f"{requested}^{{commit}}")
    return _run_git(repo_root, "log", "-1", "--format=%H", "--", str(SKILL_RELATIVE_PATH))


@contextmanager
def _materialize_skill(repo_root: Path, commit: str) -> Iterator[Path]:
    """Materialize exactly one committed skill tree, excluding worktree edits."""

    with tempfile.TemporaryDirectory(prefix="avibe-skill-") as temp_dir:
        root = Path(temp_dir)
        archive_result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "archive",
                "--format=tar",
                commit,
                "--",
                str(SKILL_RELATIVE_PATH),
            ],
            check=False,
            capture_output=True,
        )
        if archive_result.returncode != 0:
            detail = archive_result.stderr.decode(errors="replace").strip()
            raise SyncError(f"cannot read canonical skill at {commit}: {detail}")

        prefix = f"{SKILL_RELATIVE_PATH.as_posix()}/"
        with tarfile.open(fileobj=io.BytesIO(archive_result.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith(prefix):
                    continue
                relative_name = member.name[len(prefix) :]
                if not relative_name or Path(relative_name).is_absolute():
                    continue
                destination = root / relative_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise SyncError(f"cannot extract canonical skill file: {member.name}")
                destination.write_bytes(source.read())
                destination.chmod(member.mode & 0o777)

        if not (root / "SKILL.md").is_file() or not (root / "scripts/wait_pr.py").is_file():
            raise SyncError(f"canonical commit {commit} does not contain a complete {SKILL_NAME} skill")
        yield root


def _tree_sha256(skill_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in skill_root.rglob("*")
        if path.is_file()
        and path.name != MANIFEST_NAME
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(skill_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _skill_version(skill_root: Path) -> str:
    content = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*([^\s]+)\s*$", content, flags=re.MULTILINE)
    if not match:
        raise SyncError(f"{skill_root / 'SKILL.md'} has no frontmatter version")
    return match.group(1)


def _manifest(repo_root: Path, commit: str, skill_root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "skill": SKILL_NAME,
        "canonical_relative_path": SKILL_RELATIVE_PATH.as_posix(),
        "canonical_commit": commit,
        "tree_sha256": _tree_sha256(skill_root),
        "skill_version": _skill_version(skill_root),
        "canonical_path": str(repo_root / SKILL_RELATIVE_PATH),
    }


def _expanded_targets(values: list[str] | None) -> list[Path]:
    raw_targets = list(values) if values else [str(target) for target in DEFAULT_TARGETS]
    return [Path(value).expanduser() for value in raw_targets]


def _active_skill_file(repo_root: Path) -> Path:
    override = os.environ.get("BACKGROUND_WATCH_HOOK_SKILL_FILE")
    if override:
        candidates = [Path(override).expanduser()]
    else:
        candidates = [
            repo_root / SKILL_RELATIVE_PATH / "SKILL.md",
            repo_root / ".agents/skills" / SKILL_NAME / "SKILL.md",
        ]
        for variable, default in (
            ("CODEX_HOME", "~/.codex"),
            ("AGENTS_HOME", "~/.agents"),
            ("OPENCODE_HOME", "~/.opencode"),
            ("XDG_CONFIG_HOME", "~/.config"),
        ):
            root = Path(os.environ.get(variable) or default).expanduser()
            skill_root = root / "opencode/skills" if variable == "XDG_CONFIG_HOME" else root / "skills"
            candidates.append(skill_root / SKILL_NAME / "SKILL.md")

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise SyncError(f"active {SKILL_NAME} skill not found; searched: {searched}")


def _target_destination(target: Path) -> Path:
    if target.is_symlink():
        if not target.exists():
            raise SyncError(f"installation target is a broken symlink: {target}")
        return target.resolve()
    return target


def _path_key(path: Path) -> str:
    return os.path.realpath(path)


def _install_target(
    target: Path,
    source: Path,
    canonical_root: Path,
    manifest: dict[str, Any],
) -> Path:
    destination = _target_destination(target)
    if (
        destination == canonical_root
        or destination.is_relative_to(canonical_root)
        or canonical_root.is_relative_to(destination)
    ):
        raise SyncError(f"refusing to install the skill over the canonical checkout: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-", dir=destination.parent))
    backup: Path | None = None
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if destination.exists() or destination.is_symlink():
            backup = destination.with_name(f".{destination.name}.old-{os.getpid()}")
            os.replace(destination, backup)
        os.replace(staging, destination)
        staging = None
        if backup is not None:
            shutil.rmtree(backup)
        return destination
    except OSError as exc:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and not destination.exists() and backup.exists():
            os.replace(backup, destination)
        raise SyncError(f"cannot install {SKILL_NAME} into {target}: {exc}") from exc


def _check_target(target: Path, expected: dict[str, Any]) -> list[str]:
    if target.is_symlink() and not target.exists():
        return [f"{target}: broken symlink"]
    if not target.is_dir():
        return [f"{target}: installation is missing"]

    actual_root = target.resolve()
    manifest_path = actual_root / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"{target}: missing {MANIFEST_NAME}; run --install"]
    try:
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{target}: invalid {MANIFEST_NAME}: {exc}"]

    problems: list[str] = []
    for key in ("skill", "canonical_commit", "tree_sha256", "skill_version"):
        if recorded.get(key) != expected.get(key):
            problems.append(
                f"{target}: {key}={recorded.get(key)!r}, expected {expected.get(key)!r}"
            )
    try:
        actual_hash = _tree_sha256(actual_root)
        if actual_hash != expected["tree_sha256"]:
            problems.append(f"{target}: tree_sha256={actual_hash}, expected {expected['tree_sha256']}")
    except OSError as exc:
        problems.append(f"{target}: cannot hash installed skill: {exc}")

    problems.extend(_check_waiter_capabilities(actual_root))
    return problems


def _check_active_target(
    active_file: Path,
    expected: dict[str, Any],
    canonical_root: Path,
) -> list[str]:
    active_root = active_file.parent
    if active_root == canonical_root:
        try:
            actual_hash = _tree_sha256(active_root)
        except OSError as exc:
            return [f"{active_root}: cannot hash active skill: {exc}"]
        if actual_hash != expected["tree_sha256"]:
            return [
                f"{active_root}: tree_sha256={actual_hash}, "
                f"expected {expected['tree_sha256']}"
            ]
        return _check_waiter_capabilities(active_root)
    return _check_target(active_root, expected)


def _check_waiter_capabilities(skill_root: Path) -> list[str]:
    problems: list[str] = []
    wait_pr = skill_root / "scripts/wait_pr.py"
    try:
        help_result = subprocess.run(
            [sys.executable, str(wait_pr), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return [f"{skill_root}: cannot execute wait_pr.py --help: {exc}"]
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    if help_result.returncode != 0:
        problems.append(f"{skill_root}: wait_pr.py --help exited {help_result.returncode}")
    for flag in REQUIRED_WAIT_PR_FLAGS:
        if flag not in help_text:
            problems.append(f"{skill_root}: wait_pr.py --help is missing {flag}")
    return problems


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Verify the active skill or explicit installed targets without changing them",
    )
    mode.add_argument("--install", action="store_true", help="Install the canonical skill into the targets")
    parser.add_argument("--repo-root", help="Avibe repository root; defaults to this checkout")
    parser.add_argument(
        "--commit",
        help="Canonical git commit; defaults to the latest commit touching the skill tree",
    )
    parser.add_argument("--target", action="append", help="Installation target; repeat for multiple harnesses")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        repo_root = _resolve_repo_root(args.repo_root)
        commit = _resolve_commit(repo_root, args.commit)
        canonical_path = repo_root / SKILL_RELATIVE_PATH
        active_file = None
        targets = _expanded_targets(args.target) if args.install or args.target else []
        with _materialize_skill(repo_root, commit) as canonical:
            expected = _manifest(repo_root, commit, canonical)
            canonical_root = canonical_path.resolve()
            if args.check and not args.target:
                try:
                    active_file = _active_skill_file(repo_root)
                except SyncError as exc:
                    raise SyncError(
                        f"{exc}; canonical path {canonical_path} requires commit {commit}"
                    ) from exc
                targets = [active_file.parent]
            if args.install:
                installed: list[str] = []
                seen: set[str] = set()
                for target in targets:
                    destination = _target_destination(target)
                    key = _path_key(destination)
                    if key in seen:
                        continue
                    seen.add(key)
                    installed.append(str(_install_target(target, canonical, canonical_root, expected)))
                payload: dict[str, Any] = {
                    "ok": True,
                    "action": "install",
                    "skill": SKILL_NAME,
                    "canonical_path": str(canonical_path),
                    "canonical_commit": commit,
                    "tree_sha256": expected["tree_sha256"],
                    "skill_version": expected["skill_version"],
                    "targets": installed,
                }
            else:
                problems: list[str] = []
                checked: list[str] = []
                seen = set()
                for target in targets:
                    if target.is_symlink() and not target.exists():
                        destination = target
                    elif target.exists():
                        destination = _target_destination(target)
                    else:
                        destination = target
                    key = _path_key(destination)
                    if key in seen:
                        continue
                    seen.add(key)
                    checked.append(str(target))
                    if active_file is not None:
                        problems.extend(_check_active_target(active_file, expected, canonical_root))
                    else:
                        problems.extend(_check_target(target, expected))
                payload = {
                    "ok": not problems,
                    "action": "check",
                    "skill": SKILL_NAME,
                    "canonical_path": str(canonical_path),
                    "required_commit": commit,
                    "required_tree_sha256": expected["tree_sha256"],
                    "required_skill_version": expected["skill_version"],
                    "targets": checked,
                    "problems": problems,
                }
    except SyncError as exc:
        payload = {"ok": False, "action": "error", "error": str(exc)}

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        if payload.get("action") == "check":
            print(
                f"canonical {payload['canonical_path']} @ {payload['required_commit']} "
                f"(version {payload['required_skill_version']}, tree {payload['required_tree_sha256']})"
            )
            if payload["ok"]:
                for target in payload["targets"]:
                    print(f"OK {target}")
            else:
                for problem in payload["problems"]:
                    print(f"BLOCKED {problem}", file=sys.stderr)
        elif payload.get("action") == "install":
            print(
                f"installed {payload['skill']} @ {payload['canonical_commit']} "
                f"(version {payload['skill_version']}, tree {payload['tree_sha256']})"
            )
            for target in payload["targets"]:
                print(f"OK {target}")
        else:
            print(f"BLOCKED {payload['error']}", file=sys.stderr)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
