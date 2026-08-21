from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import github_release


REPO = "avibe-bot/avibe"
TAG = "v3.0.14"
SOURCE_SHA = "a" * 40


def _completed(
    arguments: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["gh", *arguments],
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _release_payload(*, draft: bool, prerelease: bool = False, body: str = "notes") -> str:
    return json.dumps(
        {
            "tag_name": TAG,
            "draft": draft,
            "prerelease": prerelease,
            "body": body,
            "html_url": f"https://github.com/{REPO}/releases/tag/{TAG}",
        }
    )


def test_get_release_treats_only_http_404_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_found(arguments: list[str], *, check: bool = True):
        assert check is False
        return _completed(arguments, returncode=1, stderr="gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(github_release, "_run_gh", not_found)
    assert github_release.get_release(REPO, TAG) is None

    def unauthorized(arguments: list[str], *, check: bool = True):
        assert check is False
        return _completed(arguments, returncode=1, stderr="gh: HTTP 401: Bad credentials\n")

    monkeypatch.setattr(github_release, "_run_gh", unauthorized)
    with pytest.raises(github_release.ReleaseError, match="Could not inspect"):
        github_release.get_release(REPO, TAG)


def test_ensure_draft_creates_a_verified_non_latest_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    release_reads = iter(
        [
            _completed([], returncode=1, stderr="gh: Not Found (HTTP 404)\n"),
            _completed([], stdout=_release_payload(draft=True)),
        ]
    )

    def fake_run(arguments: list[str], *, check: bool = True):
        calls.append(arguments)
        if arguments[0] == "api":
            return next(release_reads)
        return _completed(arguments)

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    state = github_release.ensure_draft(
        repo=REPO,
        tag=TAG,
        title=TAG,
        notes="placeholder",
        notes_file=None,
    )

    assert state.draft is True
    create = next(call for call in calls if call[:2] == ["release", "create"])
    assert create == [
        "release",
        "create",
        TAG,
        "--repo",
        REPO,
        "--title",
        TAG,
        "--notes",
        "placeholder",
        "--draft",
        "--latest=false",
        "--verify-tag",
    ]


@pytest.mark.parametrize("draft", [True, False])
def test_ensure_draft_reuses_every_existing_release_state(
    monkeypatch: pytest.MonkeyPatch,
    draft: bool,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], *, check: bool = True):
        calls.append(arguments)
        return _completed(arguments, stdout=_release_payload(draft=draft))

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    state = github_release.ensure_draft(
        repo=REPO,
        tag=TAG,
        title=TAG,
        notes="placeholder",
        notes_file=None,
    )

    assert state.draft is draft
    assert all(call[:2] != ["release", "create"] for call in calls)


def test_update_notes_never_changes_publication_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes_file = tmp_path / "release.md"
    notes_file.write_text("release notes", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], *, check: bool = True):
        calls.append(arguments)
        if arguments[0] == "api":
            return _completed(arguments, stdout=_release_payload(draft=True))
        return _completed(arguments)

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    github_release.update_notes(
        repo=REPO,
        tag=TAG,
        title=TAG,
        notes_file=notes_file,
    )

    edit = next(call for call in calls if call[:2] == ["release", "edit"])
    assert "--draft=false" not in edit
    assert "--latest" not in edit
    assert "--latest=false" not in edit
    assert "--prerelease" not in edit
    assert "--prerelease=false" not in edit


def test_wait_for_notes_requires_exact_source_success_and_ready_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_states = iter(
        [
            github_release.ReleaseState(
                tag=TAG,
                draft=True,
                prerelease=False,
                body="notes without marker",
                url="https://example.test/release",
            ),
            github_release.ReleaseState(
                tag=TAG,
                draft=True,
                prerelease=False,
                body=f"notes\n{github_release.notes_ready_marker(SOURCE_SHA, 42)}",
                url="https://example.test/release",
            ),
        ]
    )
    monkeypatch.setattr(github_release.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        github_release,
        "get_release",
        lambda _repo, _tag: next(release_states),
    )
    monkeypatch.setattr(
        github_release,
        "_get_workflow_run",
        lambda **_kwargs: {
            "id": 42,
            "status": "completed",
            "conclusion": "success",
            "head_sha": SOURCE_SHA,
            "head_branch": TAG,
            "event": "push",
            "path": ".github/workflows/release_ai.yml",
            "html_url": "https://example.test/run/42",
        },
    )

    state = github_release.wait_for_notes(
        repo=REPO,
        tag=TAG,
        workflow="release_ai.yml",
        branch=TAG,
        run_sha=SOURCE_SHA,
        source_sha=SOURCE_SHA,
        event="push",
        timeout=10,
        interval=0,
    )
    assert state.draft is True


def test_wait_for_notes_rejects_success_without_ready_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github_release,
        "get_release",
        lambda _repo, _tag: github_release.ReleaseState(
            tag=TAG,
            draft=True,
            prerelease=False,
            body="notes without marker",
            url="https://example.test/release",
        ),
    )

    with pytest.raises(github_release.ReleaseError, match="Timed out"):
        github_release.wait_for_notes(
            repo=REPO,
            tag=TAG,
            workflow="release_ai.yml",
            branch=TAG,
            run_sha=SOURCE_SHA,
            source_sha=SOURCE_SHA,
            event="push",
            timeout=0,
            interval=0,
        )


def test_wait_for_notes_rejects_the_failed_run_named_by_the_tag_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github_release,
        "get_release",
        lambda _repo, _tag: github_release.ReleaseState(
            tag=TAG,
            draft=True,
            prerelease=False,
            body=github_release.notes_ready_marker(SOURCE_SHA, 43),
            url="https://example.test/release",
        ),
    )
    monkeypatch.setattr(
        github_release,
        "_get_workflow_run",
        lambda **_kwargs: {
            "id": 43,
            "status": "completed",
            "conclusion": "failure",
            "head_sha": SOURCE_SHA,
            "head_branch": TAG,
            "event": "push",
            "path": ".github/workflows/release_ai.yml",
            "html_url": "https://example.test/run/43",
        },
    )

    with pytest.raises(github_release.ReleaseError, match="run/43"):
        github_release.wait_for_notes(
            repo=REPO,
            tag=TAG,
            workflow="release_ai.yml",
            branch=TAG,
            run_sha=SOURCE_SHA,
            source_sha=SOURCE_SHA,
            event="push",
            timeout=10,
            interval=0,
        )


def test_ready_marker_requires_an_exact_commit_sha() -> None:
    with pytest.raises(github_release.ReleaseError, match="Invalid release source SHA"):
        github_release.notes_ready_marker("main", 42)

    with pytest.raises(github_release.ReleaseError, match="Invalid release notes run ID"):
        github_release.notes_ready_marker(SOURCE_SHA, 0)


@pytest.mark.parametrize(
    ("prerelease", "latest", "latest_tag", "expected_flags"),
    [
        (False, "true", TAG, {"--draft=false", "--prerelease=false", "--latest"}),
        (True, "false", "v3.0.13", {"--draft=false", "--prerelease", "--latest=false"}),
    ],
)
def test_finalize_is_the_only_explicit_publication_transition(
    monkeypatch: pytest.MonkeyPatch,
    prerelease: bool,
    latest: str,
    latest_tag: str,
    expected_flags: set[str],
) -> None:
    calls: list[list[str]] = []
    release_reads = iter(
        [
            _completed([], stdout=_release_payload(draft=True, prerelease=prerelease)),
            _completed([], stdout=_release_payload(draft=False, prerelease=prerelease)),
        ]
    )

    def fake_run(arguments: list[str], *, check: bool = True):
        calls.append(arguments)
        if arguments[0] == "api" and arguments[1].endswith("/releases/latest"):
            return _completed(arguments, stdout=f"{latest_tag}\n")
        if arguments[0] == "api":
            return next(release_reads)
        return _completed(arguments)

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    state = github_release.finalize_release(
        repo=REPO,
        tag=TAG,
        prerelease=prerelease,
        latest=latest,
    )

    assert state.draft is False
    edit = next(call for call in calls if call[:2] == ["release", "edit"])
    assert expected_flags <= set(edit)


def test_finalize_waits_for_release_and_latest_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_reads = iter(
        [
            _completed([], stdout=_release_payload(draft=True)),
            _completed([], stdout=_release_payload(draft=True)),
            _completed([], stdout=_release_payload(draft=False)),
        ]
    )
    latest_reads = iter(["v3.0.13\n", f"{TAG}\n"])
    sleeps: list[float] = []

    def fake_run(arguments: list[str], *, check: bool = True):
        if arguments[0] == "api" and arguments[1].endswith("/releases/latest"):
            return _completed(arguments, stdout=next(latest_reads))
        if arguments[0] == "api":
            return next(release_reads)
        return _completed(arguments)

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    monkeypatch.setattr(github_release.time, "sleep", sleeps.append)

    state = github_release.finalize_release(
        repo=REPO,
        tag=TAG,
        prerelease=False,
        latest="true",
    )
    assert state.draft is False
    assert sleeps == [1, 1]


def test_release_workflows_stage_then_finalize_once() -> None:
    root = Path(__file__).resolve().parents[1]
    publish = (root / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    notes = (root / ".github/workflows/release_ai.yml").read_text(encoding="utf-8")

    assert "python release-automation/scripts/github_release.py ensure-draft" in publish
    assert "path: release-automation" in publish
    assert "ref: ${{ needs.resolve-tag.outputs.workflow_sha }}" in publish
    assert publish.index("- name: Build package") < publish.index(
        "- name: Checkout release automation"
    )
    assert "finalize-github-release:" in publish
    assert "python scripts/github_release.py wait-notes" in publish
    assert "--run-sha \"${{ needs.resolve-tag.outputs.workflow_sha }}\"" in publish
    assert "--source-sha \"${{ needs.resolve-tag.outputs.source_sha }}\"" in publish
    assert "python scripts/github_release.py finalize" in publish
    avibe_publish = publish.split("  publish-avibe-os:", 1)[1].split(
        "  publish-vibe-remote:", 1
    )[0]
    legacy_publish = publish.split("  publish-vibe-remote:", 1)[1].split(
        "  finalize-github-release:", 1
    )[0]
    assert "- finalize-github-release" in avibe_publish
    assert "- finalize-github-release" in legacy_publish

    official_step = notes.split("- name: Update official release notes", 1)[1]
    assert "python scripts/github_release.py update-notes" in official_step
    assert "python scripts/github_release.py finalize" not in official_step
    assert github_release.NOTES_READY_MARKER_PREFIX in notes
    assert "run=${GITHUB_RUN_ID}" in notes
    assert "<!-- avibe:update-notification=none -->" in notes
    assert "<!-- vibe-remote:update-notification=none -->" in notes
