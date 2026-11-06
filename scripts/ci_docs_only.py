#!/usr/bin/env python3
"""Classify a complete event diff; uncertainty selects full CI.

Only Git trees are consulted: no API file-list limits or truncated push commit
arrays. A pull request or merge-group entry targeting the default branch whose
every changed path is narrative skips the test jobs (docs/plans/ci-docs-fast-path.md).
Otherwise two job families may skip when no changed path can reach their inputs
(docs/plans/ci-affected-routing.md). Errors and anything unrecognised run
full CI; pushes are retained only for standalone exact-diff probes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Callable, NamedTuple

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

RUN, SKIP = "run", "skip"

# The browser lanes check out exactly these paths (lint.yml's sparse checkout,
# pinned equal by tests/test_ci_pipeline.py): the Web UI, plus the files Vite
# serves to it and the build's out-of-tree import check reads. Nothing else in
# the repository exists on those runners.
BROWSER_CHECKOUT = (
    "ui/",
    "vibe/message_types.json",
    "vibe/data/",
    "assets/mascot/cloud-tuanzi-v2.png",
    "Dockerfile",
)
# A change confined to these roots (outside the browser checkout) cannot reach
# a browser lane. Every Python, Vitest, build and install job still runs.
BACKEND_ROOTS = frozenset({
    "core", "modules", "vibe", "storage", "config", "tests", "skills",
    "desktop", "docs", "assets", "standards", "examples", ".agents",
})
BACKEND_FILES = frozenset({"main.py"})
# The installed-package platform checks (web-fetch-runtime and the Windows
# installer smoke) read the wheel, uv.lock, install.ps1 and Python sources and
# tests, never Web UI content. These paths reach them only as the bytes of
# vibe/ui/dist inside the wheel, which the Linux build, distribution contracts
# and install regressions still verify; the build names every module and asset
# it takes from them after its source plus a content hash. Build code and
# inputs that choose output names or paths themselves are deliberately absent:
# ui/scripts/ (Vite plugins can emit files under any name), package manifests,
# Vite/TypeScript build config, index.html and public/.
UI_SOURCE_DIRS = ("ui/src/", "ui/e2e/", "ui/design/")
UI_SOURCE_FILES = frozenset({
    "ui/.oxlintrc.json", "ui/README.md", "ui/agentation.d.ts", "ui/design.html",
    "ui/eslint-baseline.json", "ui/tsconfig.tests.json",
})
PLAYWRIGHT_CONFIG = re.compile(r"ui/playwright(?:\.[a-z0-9-]+)?\.config\.ts")
SCENARIO_CATALOG = re.compile(r"tests/scenarios/[a-z0-9_]+/catalog\.yaml")
# Names a wheel can install on Windows and macOS: no reserved device names, no
# characters or trailing dots Windows rejects, and room under MAX_PATH.
PORTABLE_SEGMENT = re.compile(r"[A-Za-z0-9._@+-]{1,64}")
WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{n}" for n in range(10)), *(f"lpt{n}" for n in range(10)),
})


class Scope(NamedTuple):
    docs_only: bool
    reason: str
    windows_control_ipc: bool
    browser: str
    browser_reason: str
    platform: str
    platform_reason: str


def full(reason: str, windows_control_ipc: bool) -> Scope:
    return Scope(False, reason, windows_control_ipc, RUN, reason, RUN, reason)


def well_formed(path: str) -> bool:
    return "\\" not in path and all(part not in ("", ".", "..") for part in path.split("/"))


def narrative_path(path: str) -> bool:
    parts = path.split("/")
    if not well_formed(path):
        return False
    if path in ROOT_DOCS:
        return True
    return (
        parts[0] == "docs" and PurePosixPath(path).suffix == ".md"
        and not path.startswith("docs/plans/") and path not in CONTRACT_DOCS
        and not any(part == "contracts" or part.endswith("-contracts") for part in parts[:-1])
    )


def in_browser_checkout(path: str) -> bool:
    return any(path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in BROWSER_CHECKOUT)


def browser_independent(path: str) -> bool:
    return (
        well_formed(path) and not in_browser_checkout(path)
        and (path in BACKEND_FILES or path.split("/", 1)[0] in BACKEND_ROOTS)
    )


def platform_independent(path: str) -> bool:
    return well_formed(path) and (
        path.startswith(("docs/", *UI_SOURCE_DIRS)) or path in UI_SOURCE_FILES
        or PLAYWRIGHT_CONFIG.fullmatch(path) is not None or SCENARIO_CATALOG.fullmatch(path) is not None
    )


def portable_path(path: str) -> bool:
    return all(
        PORTABLE_SEGMENT.fullmatch(part) and not part.endswith(".")
        and part.split(".", 1)[0].lower() not in WINDOWS_RESERVED
        for part in path.split("/")
    )


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=60,
    ).stdout


def revision(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("missing or invalid event revision")
    return value


def route(paths: list[str], independent: Callable[[str], bool], family: str) -> tuple[str, str]:
    for path in paths:
        if not (narrative_path(path) or independent(path)):
            return RUN, f"{json.dumps(path, ensure_ascii=True)} is not proven outside the {family}' inputs"
    return SKIP, f"no changed path can reach the {family}"


def scope_of(entries: list[tuple[str, str, str, str]], routable: bool) -> Scope:
    """Classify complete diff records ``(status, old mode, new mode, path)``.

    ``routable`` is true only for a pull request into the default branch; any
    other event can skip nothing beyond the documentation fast path.
    """
    paths = [path for *_, path in entries]
    windows_control_ipc = any(path in WINDOWS_CONTROL_IPC_PATHS for path in paths)
    if not entries:
        return full("no changed paths", windows_control_ipc)
    if any(status not in {"A", "M", "D"} or {old, new} - {"000000", "100644"}
           for status, old, new, _ in entries):
        return full("non-document file type or mode changed", windows_control_ipc)
    code = next((path for path in paths if not narrative_path(path)), None)
    if code is None:
        reason = f"all {len(paths)} changed paths are narrative documentation"
        return Scope(True, reason, False, SKIP, reason, SKIP, reason)
    reason = f"not documentation only: {json.dumps(code, ensure_ascii=True)}"
    if not routable:
        return full(reason, windows_control_ipc)
    browser, browser_reason = route(paths, browser_independent, "browser lanes")
    platform, platform_reason = route(paths, platform_independent, "installed-package platform checks")
    if platform == SKIP:
        # No rename detection: a rename is a deletion plus this added path.
        added = next((path for status, _, _, path in entries if status == "A" and not portable_path(path)), None)
        if added is not None:
            platform, platform_reason = RUN, f"added name {json.dumps(added, ensure_ascii=True)} is not portable"
    return Scope(False, reason, windows_control_ipc, browser, browser_reason, platform, platform_reason)


def targets_default_branch(event_name: str, event: dict) -> bool:
    repository = event.get("repository")
    default = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(default, str) or not default:
        return False
    if event_name == "pull_request":
        pr = event.get("pull_request")
        base = pr.get("base") if isinstance(pr, dict) else None
        ref = base.get("ref") if isinstance(base, dict) else None
    elif event_name == "merge_group":
        group = event.get("merge_group")
        ref = group.get("base_ref") if isinstance(group, dict) else None
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            ref = ref.removeprefix("refs/heads/")
    else:
        return False
    return isinstance(ref, str) and bool(ref) and ref == default


def object_exists(repo: Path, revision_name: str) -> None:
    try:
        git(repo, "cat-file", "-e", f"{revision_name}^{{commit}}")
    except subprocess.CalledProcessError:
        # A shallow checkout may not contain the validated base object. Fetch
        # that exact object, never a moving ref or an event-provided name.
        git(repo, "fetch", "--no-tags", "--depth=1", "origin", revision_name)


def classify(event_name: str, event: dict, sha: str, repo: Path) -> Scope:
    try:
        if not isinstance(event, dict):
            raise ValueError("event payload is not an object")
        checked_out_sha = revision(git(repo, "rev-parse", "HEAD").decode().strip())
        if event_name == "pull_request":
            # actions/checkout uses the synthetic merge ref, while GITHUB_SHA may
            # identify the PR source head. The event's base/head parent check below
            # is the authority that proves this checkout is the intended merge.
            sha = checked_out_sha
            pr = event["pull_request"]
            base, head = revision(pr["base"]["sha"]), revision(pr["head"]["sha"])
            header = git(repo, "cat-file", "commit", sha).split(b"\n\n", 1)[0]
            parents = [line[7:].decode() for line in header.splitlines() if line.startswith(b"parent ")]
            if parents != [base, head]:
                raise ValueError("checkout is not the event's synthetic merge")
        elif event_name == "merge_group":
            # The queue checkout is the exact merge-group head. Unlike a PR
            # merge ref, the event gives us the base/head pair to diff; this is
            # what keeps affected-job routing valid for a queue group.
            sha = revision(sha)
            if checked_out_sha != sha:
                raise ValueError("checkout does not match event SHA")
            group = event["merge_group"]
            base = revision(group["base_sha"])
            if revision(group["head_sha"]) != sha:
                raise ValueError("checkout does not match merge-group head SHA")
            object_exists(repo, base)
        elif event_name == "push":
            sha = revision(sha)
            if checked_out_sha != sha:
                raise ValueError("checkout does not match event SHA")
            if any(event.get(flag) is not False for flag in ("created", "deleted", "forced")):
                raise ValueError("new, deleted, forced or incomplete push event")
            base = revision(event["before"])
            if revision(event["after"]) != sha:
                raise ValueError("push after does not match checkout")
            object_exists(repo, base)
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
        entries = []
        for header, name in zip(fields[::2], fields[1::2]):
            meta = header.decode("ascii").split()
            if len(meta) != 5 or not meta[0].startswith(":"):
                raise ValueError("unexpected diff record")
            entries.append((meta[4], meta[0][1:], meta[1], name.decode("utf-8")))
        routable = event_name in {"pull_request", "merge_group"} and targets_default_branch(event_name, event)
        return scope_of(entries, routable)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Do not print Git stderr, event payloads or arbitrary commit data.
        # Conservatively select the Windows job when classification cannot prove
        # the Skill publication paths are absent from this event.
        return full(f"full CI: classification unavailable ({type(exc).__name__})", True)


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        event = {}
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    scope = classify(event_name, event, os.environ.get("GITHUB_SHA", ""), Path.cwd())
    # Merge-group scope is derived above from its validated base/head pair;
    # there is no second master-push pass to widen it.
    # Output/write failures remain job failures; consumers require success AND
    # literal dispositions, so no missing output can authorize a skip.
    outputs = {
        "docs_only": str(scope.docs_only).lower(),
        "windows_control_ipc": str(scope.windows_control_ipc).lower(),
        "browser": scope.browser,
        "platform": scope.platform,
    }
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.writelines(f"{name}={value}\n" for name, value in outputs.items())
    print(f"docs_only={outputs['docs_only']}: {scope.reason}")
    print(f"windows_control_ipc={outputs['windows_control_ipc']}")
    print(f"browser={scope.browser}: {scope.browser_reason}")
    print(f"platform={scope.platform}: {scope.platform_reason}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        if scope.docs_only:
            title = "documentation only"
        elif SKIP in (scope.browser, scope.platform):
            title = "routed"
        else:
            title = "full"
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(
                f"## CI scope: {title}\n\n{scope.reason}\n\n"
                f"- Browser lanes: **{scope.browser}**, {scope.browser_reason}\n"
                f"- Installed-package platform checks: **{scope.platform}**, {scope.platform_reason}\n"
            )


if __name__ == "__main__":
    main()
