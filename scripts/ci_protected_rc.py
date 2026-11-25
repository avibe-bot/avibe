"""Which native proofs an RC range must supply; unknown inputs require every group.

Job names here refer to jobs within the exact-SHA master lint workflow, not arbitrary
check contexts. Classify both sides of a rename, and every commit since publication.
"""

from __future__ import annotations

import html
import subprocess
from pathlib import Path

from scripts.ci_docs_only import CLI_CONTRACT_DOCS, narrative_path

ROOT = Path(__file__).resolve().parents[1]

# The terminal flags of the ordinary PR are not substitutes for native evidence.
# Select producers, not their fan-in gates: one lost runner also makes its
# aggregators red. The independent approved exception applies to a producer only.
UNIT = frozenset(f"unit-tests (Python 3.12, {shard}/8)" for shard in range(8))
IDENTITY = UNIT | frozenset({"show-router-integration", "windows-control-ipc", "ui-browser-checks (3)"})
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
        if narrative_path(path):
            continue
        if path in CLI_CONTRACT_DOCS:
            # The planner can move the CLI and harness contract tests between shards.
            jobs.update(UNIT)
        elif path.startswith("storage/"):
            # MigrationFileLock is used by the real native Model Hub installer.
            # Storage is shared: do not infer unaffected producers from its directory.
            jobs.update(ALL)
        elif path.startswith("desktop/"):
            jobs.update(DESKTOP | FETCH | INSTALL | IDENTITY)
        elif path.startswith(("core/handlers/model_hub/", "vibe/model_hub_runtime/")):
            jobs.update(NATIVE | FETCH | INSTALL | IDENTITY)
        else:
            jobs.update(ALL)
    return jobs


NOTE_PREFIX = 'Runner exception (release lead, quoted Product approval): <code>'


def exception_note(value: str) -> str:
    return NOTE_PREFIX + html.escape(value) + '</code>'


def preserve_exception_notes(generated: str, existing: str, waiver: str) -> str:
    # Only the exact release's existing body and the structured dispatch input
    # can supply this audit line. Generated prose cannot create an authorization.
    fresh = [line for line in generated.splitlines() if not line.startswith(NOTE_PREFIX)]
    preserved = [line for line in existing.splitlines() if line.startswith(NOTE_PREFIX) and line.endswith('</code>')]
    if waiver:
        preserved.append(exception_note(waiver))
    return '\n'.join(fresh + ([''] + list(dict.fromkeys(preserved)) if preserved else [])) + '\n'


def inputs_changed(name: str, paths: set[str]) -> bool:
    """A stricter question than range classification: could this producer read the delta?"""
    if name == "install-upgrade-regression (installer, macos-15)":
        # This producer installs the Linux wheel and exercises the Python installer
        # and native listener. The iOS Xcode project and its test targets are not
        # packaged or used there. Do not extend this exemption to unknown roots.
        paths = {p for p in paths if not (p == "ios/Avibe.xcodeproj/project.pbxproj" or p.startswith("ios/AvibeTests/"))}
    return name in required_jobs(paths)


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
