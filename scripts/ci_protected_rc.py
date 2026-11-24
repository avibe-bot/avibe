"""Which native proofs an RC range must supply; unknown inputs require every group.

Job names here refer to jobs within the exact-SHA master lint workflow, not arbitrary
check contexts. Classify both sides of a rename, and every commit since publication.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The terminal flags of the ordinary PR are not substitutes for native evidence.
# Select producers, not their fan-in gates: one lost runner also makes its
# aggregators red. The independent approved exception applies to a producer only.
IDENTITY = frozenset(
    {"show-router-integration", "windows-control-ipc", "ui-browser-checks (3)"}
    | {f"unit-tests (Python 3.12, {shard}/8)" for shard in range(8)}
)
NATIVE = frozenset(
    {
        f"model-hub-lifecycle-native ({runner})"
        for runner in (
            "ubuntu-latest",
            "ubuntu-24.04-arm",
            "windows-latest",
            "windows-11-arm",
            "macos-15",
            "macos-15-intel",
        )
    }
)
DESKTOP = frozenset({"desktop-shell / windows-latest", "desktop-shell / macos-26"})
INSTALL = frozenset(
    {f"install-upgrade-regression (installer, {runner})" for runner in ("ubuntu-latest", "macos-15")}
    | {f"windows-install-smoke ({runner})" for runner in ("windows-latest", "windows-11-arm")}
)
MIGRATIONS = frozenset({"migration-release-guard"})
FETCH = frozenset(
    {f"web-fetch-macos / web-fetch-runtime ({runner}, 3.12)" for runner in ("macos-15", "macos-15-intel")}
)
ALL = IDENTITY | NATIVE | DESKTOP | INSTALL | MIGRATIONS | FETCH


def required_jobs(paths: set[str]) -> set[str]:
    """Only these disjoint roots can narrow the proof; shared tooling is fail-closed."""
    jobs: set[str] = set()
    for path in paths:
        if (
            path.startswith("docs/")
            and path.endswith(".md")
            and not path.startswith(("docs/plans/", "docs/contracts/"))
        ):
            continue
        if path.startswith("storage/"):
            jobs.update(MIGRATIONS | INSTALL | IDENTITY)
        elif path.startswith("desktop/"):
            jobs.update(DESKTOP | INSTALL | IDENTITY)
        elif path.startswith(("core/handlers/model_hub/", "vibe/model_hub_runtime/")):
            jobs.update(NATIVE | FETCH | INSTALL | IDENTITY)
        else:
            jobs.update(ALL)
    return jobs


def touched(base: str, source: str) -> set[str]:
    """Walk each first-parent tree delta, not just the endpoint diff (a revert counts)."""

    # SHAs have already been validated by the release resolver. No shell is used.
    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, timeout=60).stdout

    if base == source:
        return set()
    git("merge-base", "--is-ancestor", base, source)
    # Reject a side-branch base that is an ancestor only through a merge parent.
    commits = git("rev-list", "--first-parent", f"{base}..{source}").decode("ascii").splitlines()
    if commits and git("rev-parse", f"{commits[-1]}^1").decode("ascii").strip() != base:
        raise ValueError("published baseline is not on the candidate's first-parent history")
    data = git(
        "log",
        "--first-parent",
        "--diff-merges=first-parent",
        "--no-renames",
        "--format=",
        "--name-only",
        "-z",
        f"{base}..{source}",
        "--",
    )
    return {name.decode("utf-8", errors="surrogateescape") for name in data.split(b"\0") if name}
