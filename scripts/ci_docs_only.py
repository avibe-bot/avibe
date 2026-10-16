#!/usr/bin/env python3
"""Prove a complete event diff is narrative-only; uncertainty selects full CI.

Only Git trees are consulted: no API file-list limits or truncated push commit
arrays. See docs/plans/ci-docs-fast-path.md for the skip/required-check contract.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT_DOCS = frozenset({
    "AGENTS.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
    "VISION.md", "VISION_ZH.md", ".github/PULL_REQUEST_TEMPLATE.md",
})
# Installed CLI documentation is executable contract input. All plans stay
# full: tests also glob plan prose, so a list of individual exceptions cannot
# safely describe that boundary. AGENTS.md has a dedicated lightweight check.
CONTRACT_DOCS = frozenset({"docs/INSTALL_FOR_AI.md", "docs/INSTALL_FOR_AI_ZH.md"})
# Built-in Skill publication compares directory identities. Windows DirEntry
# listings zero those identities, so that path must run on the real Windows job.
WINDOWS_CONTROL_IPC_PATHS = frozenset({
    "core/managed_skills.py",
    "tests/test_managed_skills.py",
})


def narrative_path(path: str) -> bool:
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in path:
        return False
    if path in ROOT_DOCS:
        return True
    return (
        parts[0] == "docs" and PurePosixPath(path).suffix == ".md"
        and not path.startswith("docs/plans/") and path not in CONTRACT_DOCS
        and not any(part == "contracts" or part.endswith("-contracts") for part in parts[:-1])
    )


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=60,
    ).stdout


def revision(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("missing or invalid event revision")
    return value


def impact_category(paths: list[str]) -> str:
    """Path-area telemetry only, not evidence that other checks can be skipped.

    An API file under ui/src can affect Python consumers and vice versa. These
    labels deliberately say where code changed, not which tests are safe to omit.
    """
    code = [path for path in paths if not narrative_path(path)]
    if not paths:
        return "unknown"
    if not code:
        return "docs-only"
    if all(path.startswith("ui/src/") and PurePosixPath(path).suffix in {".ts", ".tsx", ".css"}
           for path in code):
        return "ui-source-only"
    if all(path.split("/", 1)[0] in {"core", "modules", "vibe", "storage", "config", "tests"}
           and path.endswith(".py") for path in code):
        return "python-source-only"
    return "shared-or-mixed"


def classify(event_name: str, event: dict, sha: str, repo: Path) -> tuple[bool, str, str, bool]:
    """Return docs disposition, reason, impact telemetry, and Windows IPC selection."""
    try:
        if not isinstance(event, dict):
            raise ValueError("event payload is not an object")
        sha = revision(sha)
        if git(repo, "rev-parse", "HEAD").decode().strip() != sha:
            raise ValueError("checkout does not match event SHA")
        if event_name == "pull_request":
            pr = event["pull_request"]
            base, head = revision(pr["base"]["sha"]), revision(pr["head"]["sha"])
            header = git(repo, "cat-file", "commit", sha).split(b"\n\n", 1)[0]
            parents = [line[7:].decode() for line in header.splitlines() if line.startswith(b"parent ")]
            if parents != [base, head]:
                raise ValueError("checkout is not the event's synthetic merge")
        elif event_name == "push":
            if any(event.get(flag) is not False for flag in ("created", "deleted", "forced")):
                raise ValueError("new, deleted, forced or incomplete push event")
            base = revision(event["before"])
            if revision(event["after"]) != sha:
                raise ValueError("push after does not match checkout")
            try:
                git(repo, "cat-file", "-e", f"{base}^{{commit}}")
            except subprocess.CalledProcessError:
                # A depth-two checkout may not contain before on a multi-commit
                # push. Fetch that exact validated object, not an untrusted ref.
                git(repo, "fetch", "--no-tags", "--depth=1", "origin", base)
        else:
            raise ValueError("unsupported event")

        # No rename detection: code renamed to *.md must still include the
        # deleted code path. NUL framing preserves spaces, tabs and Unicode.
        raw = git(repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev",
                  "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", base, sha, "--")
        if not raw or not raw.endswith(b"\0"):
            raise ValueError("empty or incomplete diff")
        fields = raw[:-1].split(b"\0")
        if len(fields) % 2:
            raise ValueError("incomplete diff records")
        paths = []
        irregular = False
        for header, name in zip(fields[::2], fields[1::2]):
            meta = header.decode("ascii").split()
            if len(meta) != 5 or not meta[0].startswith(":"):
                raise ValueError("unexpected diff record")
            old_mode, new_mode = meta[0][1:], meta[1]
            path = name.decode("utf-8")
            paths.append(path)
            if meta[4] not in {"A", "M", "D"} or {old_mode, new_mode} - {"000000", "100644"}:
                irregular = True
        windows_control_ipc = any(path in WINDOWS_CONTROL_IPC_PATHS for path in paths)
        if irregular:
            return False, "non-document file type or mode changed", "unknown", windows_control_ipc
        impact = impact_category(paths)
        for path in paths:
            if not narrative_path(path):
                return False, f"full CI required by {json.dumps(path, ensure_ascii=True)}", impact, windows_control_ipc
        return True, f"all {len(paths)} changed paths are narrative documentation", impact, False
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Do not print Git stderr, event payloads or arbitrary commit data.
        # Conservatively select the Windows job when classification cannot prove
        # the Skill publication paths are absent from this event.
        return False, f"full CI: classification unavailable ({type(exc).__name__})", "unknown", True


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        event = {}
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    docs_only, reason, impact, windows_control_ipc = classify(
        event_name, event, os.environ.get("GITHUB_SHA", ""), Path.cwd(),
    )
    # A master push supersedes the preceding run even when only docs changed.
    # It must cover code inherited from that cancelled run, not merely its own
    # before..after diff. PRs retain the reviewed documentation fast path.
    if event_name == "push" and docs_only:
        docs_only = False
        reason = "latest master push validates the whole tree, including superseded code changes"
    # Output/write failures remain job failures; consumers require success AND
    # a literal disposition, so no missing output can authorize a skip.
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"docs_only={str(docs_only).lower()}\n")
            stream.write(f"windows_control_ipc={str(windows_control_ipc).lower()}\n")
    print(f"docs_only={str(docs_only).lower()}: {reason}")
    print(f"windows_control_ipc={str(windows_control_ipc).lower()}")
    print(f"Impact shadow: {impact} (observation only; no test selection)")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(f"## CI scope: {'documentation only' if docs_only else 'full'}\n\n{reason}\n")
            # Keep shadow out of GITHUB_OUTPUT; no downstream job may use it as
            # skip authority without a separately reviewed coverage contract.
            stream.write(f"\nImpact shadow: **{impact}** (observation only; no test selection).\n")


if __name__ == "__main__":
    main()
