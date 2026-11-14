#!/usr/bin/env python3
"""Read-only manifest, workflow, and tracked JSON Schema checks for the fast CI lane.

Requires uv, npm, Cargo, actionlint 1.7.12, and the existing Python dependencies
jsonschema and PyYAML. Install tools in the workflow, not here. No native compilation or
package lifecycle scripts run. Cache uv and Cargo registries for the lock checks;
Cargo metadata deliberately resolves dependencies (never use --no-deps).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ACTIONLINT_VERSION = "1.7.12"
# The CI/release boundary. Other workflows still receive YAML syntax validation;
# extending semantic coverage must account for their runner/tool contracts.
WORKFLOW_NAMES = {
    "lint.yml",
    "ios-shell.yml",
    "desktop-shell.yml",
    "desktop-native-update-acceptance.yml",
    "web-fetch-runtime.yml",
    "release_ai.yml",
    "publish.yml",
}
# These are JSONC, validated by tsc in the UI lane, not strict JSON documents.
JSONC_FILES = {"ui/tsconfig.app.json", "ui/tsconfig.node.json"}
LOCKFILES = ("uv.lock", "ui/package-lock.json", "desktop/package-lock.json", "desktop/Cargo.lock")
MANIFESTS = (
    "pyproject.toml",
    "ui/package.json",
    "desktop/package.json",
    "desktop/Cargo.toml",
    "npm/avibe/package.json",
)


class GuardError(Exception):
    """A failed check, with actionable command/file context."""


def command(args: list[str], cwd: Path, timeout: float) -> str:
    try:
        result = subprocess.run(
            args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError(f"{cwd}: {' '.join(args)}: {exc}") from exc
    if result.returncode:
        detail = (result.stdout + result.stderr)[-16000:].strip()
        raise GuardError(f"{cwd}: {' '.join(args)} exited {result.returncode}\n{detail}")
    return result.stdout


def tracked_files(root: Path) -> list[str]:
    return command(["git", "ls-files", "-z"], root, 10).rstrip("\0").split("\0")


def read_json(path: Path) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"non-JSON constant {value}")

    try:
        return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=constant)
    except (OSError, ValueError) as exc:
        raise GuardError(f"{path}: {exc}") from exc


def snapshot(root: Path, names: set[str]) -> dict[str, str | None]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() if (root / name).exists() else None
        for name in sorted(names)
    }


def check_npm_entrypoint(root: Path) -> None:
    """The dependency-free launcher intentionally has no npm lockfile."""
    directory = root / "npm/avibe"
    manifest = read_json(directory / "package.json")
    if not isinstance(manifest, dict):
        raise GuardError("npm/avibe/package.json must be an object")
    if manifest.get("name") != "@avibe/cli" or not re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", str(manifest.get("version", ""))
    ):
        raise GuardError("npm/avibe/package.json must declare @avibe/cli and a semantic version")
    for field in (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
        "bundledDependencies",
        "bundleDependencies",
        "workspaces",
    ):
        if manifest.get(field):
            raise GuardError(f"npm/avibe declares {field}; its dependency-free/no-lock exception must be revisited")
    bins = manifest.get("bin")
    if not isinstance(bins, dict) or not {"avibe", "vibe"}.issubset(bins):
        raise GuardError("npm/avibe/package.json must declare the avibe and vibe binaries")
    for name, target in bins.items():
        if not isinstance(target, str):
            raise GuardError(f"npm/avibe binary {name!r} must name a file")
        path = (directory / target).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise GuardError(f"npm/avibe binary {name!r} points outside the package or to a missing file: {target}")
    for name in ("package-lock.json", "npm-shrinkwrap.json"):
        if (directory / name).exists():
            raise GuardError(f"npm/avibe/{name}: dependency-free launcher should not acquire a lockfile")


def check_locks(root: Path, timeout: float = 60) -> None:
    for name in (*MANIFESTS, *LOCKFILES):
        if not (root / name).is_file():
            raise GuardError(f"missing required manifest/lock: {name}; CI never generates locks")
    names = set(MANIFESTS) | set(LOCKFILES) | {"npm/avibe/package-lock.json", "npm/avibe/npm-shrinkwrap.json"}
    names.update(name for name in tracked_files(root) if name.startswith("desktop/") and name.endswith("/Cargo.toml"))
    before = snapshot(root, names)
    errors = []
    try:
        check_npm_entrypoint(root)
        checks = [
            (["uv", "lock", "--check"], root),
            (["cargo", "metadata", "--locked", "--format-version", "1"], root / "desktop"),
        ]
        checks.extend(
            (["npm", "ci", "--ignore-scripts", "--dry-run", "--no-audit", "--no-fund"], root / directory)
            for directory in ("ui", "desktop")
        )
        # Four independent resolvers, no build jobs or native compilation.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(command, args, cwd, timeout) for args, cwd in checks]
            for future in as_completed(futures):
                try:
                    future.result()
                except GuardError as exc:
                    errors.append(str(exc))
    except GuardError as exc:
        errors.append(str(exc))
    finally:
        after = snapshot(root, names)
        changed = [name for name in before if before[name] != after[name]]
        if changed:
            errors.append("check modified manifests/locks (not restored or regenerated): " + ", ".join(changed))
    if errors:
        raise GuardError("\n\n".join(errors))


def check_schemas(root: Path) -> None:
    from jsonschema import validators
    from jsonschema.exceptions import SchemaError, ValidationError

    tracked = set(tracked_files(root))
    paths = sorted(name for name in tracked if name.endswith(".schema.json"))
    if not paths:
        raise GuardError("no tracked *.schema.json files found")
    errors = []
    for name in paths:
        try:
            schema = read_json(root / name)
            if not isinstance(schema, dict) or not isinstance(schema.get("$schema"), str):
                raise GuardError(f"{name}: schema must declare its $schema dialect")
            validator = validators.validator_for(schema, default=None)
            if validator is None:
                raise GuardError(f"{name}: unsupported $schema dialect {schema['$schema']!r}")
            validator.check_schema(schema)
            # A schema and its data can each pass separate PRs yet disagree in
            # the combined tree. Validate tracked same-name companion data too.
            companion = name.removesuffix(".schema.json") + ".json"
            if companion in tracked:
                validator(schema).validate(read_json(root / companion))
        except (GuardError, SchemaError, ValidationError) as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise GuardError("\n\n".join(errors))


def read_yaml(path: Path) -> object:
    import yaml

    # BaseLoader keeps `on` a string (PyYAML's YAML 1.1 SafeLoader turns it into
    # True) and never constructs custom Python objects.
    class UniqueLoader(yaml.BaseLoader):
        def construct_mapping(self, node, deep=False):
            mapping = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in mapping:
                    raise GuardError(f"{path}: duplicate or non-scalar YAML key {key!r}")
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    try:
        return yaml.load(path.read_text(), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise GuardError(f"{path}: {exc}") from exc


def check_serialized_files(root: Path) -> None:
    errors = []
    for name in tracked_files(root):
        try:
            if name.endswith(".json") and name not in JSONC_FILES:
                read_json(root / name)
            elif name.endswith((".yml", ".yaml")):
                read_yaml(root / name)
        except GuardError as exc:
            errors.append(str(exc))
    if errors:
        raise GuardError("\n\n".join(errors))


def check_publish_queue(root: Path) -> None:
    """Cover the one GitHub field not yet understood by pinned actionlint.

    GitHub's workflow-syntax concurrency reference permits single|max, and
    forbids queue:max together with cancel-in-progress:true. Keep the exception
    bounded to literal false/omitted cancellation, as used by publish.yml.
    """
    path = root / ".github/workflows/publish.yml"
    if not path.exists():
        return
    workflow = read_yaml(path)
    if not isinstance(workflow, dict):
        raise GuardError(f"{path}: workflow must be a mapping")
    scopes = [workflow]
    jobs = workflow.get("jobs", {})
    if isinstance(jobs, dict):
        scopes.extend(job for job in jobs.values() if isinstance(job, dict))
    for scope in scopes:
        concurrency = scope.get("concurrency")
        if not isinstance(concurrency, dict) or "queue" not in concurrency:
            continue
        queue = concurrency["queue"]
        if queue not in ("single", "max"):
            raise GuardError(f"{path}: concurrency.queue must be single or max")
        if queue == "max" and concurrency.get("cancel-in-progress", "false") != "false":
            raise GuardError(f"{path}: queue:max requires cancel-in-progress to be omitted or literal false")


def check_workflows(root: Path, timeout: float = 60) -> None:
    workflow_files = [
        name
        for name in tracked_files(root)
        if name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))
    ]
    for name in workflow_files:
        read_yaml(root / name)
    workflows = [name for name in workflow_files if Path(name).name in WORKFLOW_NAMES]
    if not workflows:
        raise GuardError("no tracked CI/release workflows found")
    check_publish_queue(root)
    version = command(["actionlint", "--version"], root, timeout).splitlines()
    if not version or version[0] != ACTIONLINT_VERSION:
        raise GuardError(f"actionlint {ACTIONLINT_VERSION} required; found {version[:1]}")
    # Shell/Python lint is owned by other lanes; actionlint validates YAML,
    # workflow structure, expressions, job dependencies, and action contracts.
    command(["actionlint", "-shellcheck=", "-pyflakes=", *workflows], root, timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--timeout", type=float, default=60, help="per-tool timeout in seconds (default: 60)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--locks-only", action="store_true")
    modes.add_argument("--schemas-only", action="store_true")
    modes.add_argument("--workflows-only", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    root = args.root.resolve()
    checks = []
    if not (args.schemas_only or args.workflows_only):
        checks.append(("locks", lambda: check_locks(root, args.timeout)))
    if not (args.locks_only or args.workflows_only):
        checks.append(("JSON/YAML syntax", lambda: check_serialized_files(root)))
        checks.append(("schemas", lambda: check_schemas(root)))
    if not (args.locks_only or args.schemas_only):
        checks.append(("workflows", lambda: check_workflows(root, args.timeout)))
    failed = False
    for name, check in checks:
        started = time.monotonic()
        try:
            check()
        except (GuardError, OSError, ImportError) as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            failed = True
        else:
            print(f"PASS {name} ({time.monotonic() - started:.1f}s)", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
