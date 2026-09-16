from __future__ import annotations

import json
from itertools import permutations
import subprocess
from pathlib import Path

import pytest

from scripts import github_release


REPO = "avibe-bot/avibe"
TAG = "v3.0.14"
SOURCE_SHA = "a" * 40


@pytest.mark.parametrize("tag", ["v3.2.0-rc1", "v03.02.00", "v3.2.0.post01"])
@pytest.mark.parametrize("operation", ["ensure", "notes", "finalize"])
def test_noncanonical_official_tag_fails_before_any_release_request(monkeypatch, tmp_path, tag, operation):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid publication identity must fail before any GitHub request")

    monkeypatch.setattr(github_release, "_run_gh", forbidden)
    with pytest.raises(github_release.ReleaseError, match="canonical spelling"):
        if operation == "ensure":
            github_release.ensure_draft(repo=REPO, tag=tag, title=tag, notes="notes", notes_file=None)
        elif operation == "notes":
            github_release.update_notes(repo=REPO, tag=tag, title=tag, notes_file=tmp_path / "notes.md")
        else:
            github_release.finalize_release(repo=REPO, tag=tag, prerelease=True, latest="false")


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


def _release_pages(*payloads: str) -> str:
    return json.dumps([[json.loads(payload) for payload in payloads]])


def test_get_release_treats_only_http_404_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = iter(
        [
            _completed([], returncode=1, stderr="gh: Not Found (HTTP 404)\n"),
            _completed([], stdout=_release_pages()),
        ]
    )

    def not_found(arguments: list[str], *, check: bool = True):
        assert check is False
        return next(reads)

    monkeypatch.setattr(github_release, "_run_gh", not_found)
    assert github_release.get_release(REPO, TAG) is None

    def unauthorized(arguments: list[str], *, check: bool = True):
        assert check is False
        return _completed(arguments, returncode=1, stderr="gh: HTTP 401: Bad credentials\n")

    monkeypatch.setattr(github_release, "_run_gh", unauthorized)
    with pytest.raises(github_release.ReleaseError, match="Could not inspect"):
        github_release.get_release(REPO, TAG)


def test_get_release_falls_back_to_paginated_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    reads = iter(
        [
            _completed([], returncode=1, stderr="gh: Not Found (HTTP 404)\n"),
            _completed([], stdout=_release_pages(_release_payload(draft=True))),
        ]
    )

    def fake_run(arguments: list[str], *, check: bool = True):
        calls.append(arguments)
        return next(reads)

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    state = github_release.get_release(REPO, TAG)

    assert state is not None and state.draft is True
    assert calls[1] == [
        "api",
        "--paginate",
        "--slurp",
        f"repos/{REPO}/releases?per_page=100",
    ]


def test_get_release_rejects_duplicate_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = iter(
        [
            _completed([], returncode=1, stderr="gh: Not Found (HTTP 404)\n"),
            _completed(
                [],
                stdout=_release_pages(
                    _release_payload(draft=True),
                    _release_payload(draft=True),
                ),
            ),
        ]
    )
    monkeypatch.setattr(
        github_release,
        "_run_gh",
        lambda _arguments, check=True: next(reads),
    )

    with pytest.raises(github_release.ReleaseError, match="Multiple GitHub Releases"):
        github_release.get_release(REPO, TAG)


def test_ensure_draft_creates_a_verified_non_latest_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    release_reads = iter(
        [
            _completed([], returncode=1, stderr="gh: Not Found (HTTP 404)\n"),
            _completed([], stdout=_release_pages()),
            _completed([], returncode=1, stderr="gh: Not Found (HTTP 404)\n"),
            _completed([], stdout=_release_pages(_release_payload(draft=True))),
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


@pytest.mark.parametrize("newer", [False, True])
def test_auto_latest_uses_all_official_versions_instead_of_engine_latest(monkeypatch, newer):
    calls = []
    selected_latest = "model-hub-engine-v7.2.149-1"
    release_reads = iter([_release_payload(draft=True), _release_payload(draft=False)])
    releases = [
        {"tag_name": selected_latest, "draft": False, "prerelease": False},
        {"tag_name": "v3.0.13", "draft": False, "prerelease": False},
        {"tag_name": "v99.0.0", "draft": True, "prerelease": False},
        {"tag_name": "v88.0.0rc1", "draft": False, "prerelease": False},
    ]
    if newer:
        releases.append({"tag_name": "v3.1.0", "draft": False, "prerelease": False})

    def fake_run(arguments, *, check=True):
        nonlocal selected_latest
        calls.append(arguments)
        if "--paginate" in arguments:
            assert "--slurp" in arguments
            return _completed(arguments, stdout=json.dumps([releases[:2], releases[2:]]))
        if arguments[:2] == ["release", "edit"]:
            if "--latest" in arguments:
                selected_latest = TAG
            return _completed(arguments)
        if arguments[1].endswith("/releases/latest"):
            return _completed(arguments, stdout=selected_latest + "\n")
        return _completed(arguments, stdout=next(release_reads))

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    state = github_release.finalize_release(repo=REPO, tag=TAG, prerelease=False, latest="auto")
    assert not state.draft
    edit = next(call for call in calls if call[:2] == ["release", "edit"])
    assert ("--latest" in edit) is not newer
    assert ("--latest=false" in edit) is newer
    assert calls.index(edit) > next(i for i, call in enumerate(calls) if "--paginate" in call)


@pytest.mark.parametrize("order", list(permutations(("v3.0.14", "v3.0.15", "v3.1.0"))))
def test_serialized_finalizers_keep_highest_version_for_every_admission_order(monkeypatch, order):
    # All releases start as drafts, as with overlapping push/dispatch builds.
    # The workflow concurrency contract admits one complete finalizer at a time.
    releases = {
        tag: {
            "tag_name": tag, "draft": True, "prerelease": False, "body": "notes",
            "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        }
        for tag in order
    }
    latest = "model-hub-engine-v7.2.149-1"
    writes = []
    inventories = []

    def fake_run(arguments, *, check=True):
        nonlocal latest
        if arguments[:2] == ["release", "edit"]:
            tag = arguments[2]
            assert arguments[3:5] == ["--repo", REPO]
            assert "--draft=false" in arguments
            releases[tag]["draft"] = False
            if "--latest" in arguments:
                latest = tag
            else:
                assert "--latest=false" in arguments
            writes.append(tag)
            return _completed(arguments)
        assert arguments[0] == "api"
        if "--paginate" in arguments:
            inventories.append({tag for tag, item in releases.items() if not item["draft"]})
            return _completed(arguments, stdout=json.dumps([list(releases.values())]))
        if arguments[1].endswith("/releases/latest"):
            return _completed(arguments, stdout=latest + "\n")
        tag = arguments[1].rsplit("/", 1)[-1]
        if releases[tag]["draft"]:
            assert not check
            return _completed(arguments, returncode=1, stderr="gh: Not Found (HTTP 404)")
        return _completed(arguments, stdout=json.dumps(releases[tag]))

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    admitted = set()
    for tag in order:
        previous_reads = len(inventories)
        state = github_release.finalize_release(repo=REPO, tag=tag, prerelease=False, latest="auto")
        assert state.tag == tag and not state.draft
        # Both the draft fallback and auto-Latest decision read fresh state.
        assert inventories[previous_reads:] == [admitted, admitted]
        admitted = admitted | {tag}
        assert latest == max(admitted, key=github_release.official_stable_version_key)
    assert writes == list(order)
    assert latest == "v3.1.0"


@pytest.mark.parametrize("payload", ["not json", "{}", "[{}]", "[[null]]",
                                      '[[{"tag_name":"v3.1.0"}]]'])
def test_auto_latest_does_not_publish_on_unreadable_selection(monkeypatch, payload):
    calls = []

    def fake_run(arguments, *, check=True):
        calls.append(arguments)
        if "--paginate" in arguments:
            return _completed(arguments, stdout=payload)
        return _completed(arguments, stdout=_release_payload(draft=True))

    monkeypatch.setattr(github_release, "_run_gh", fake_run)
    with pytest.raises(github_release.ReleaseError, match="Cannot select latest"):
        github_release.finalize_release(repo=REPO, tag=TAG, prerelease=False, latest="auto")
    assert not any(call[:2] == ["release", "edit"] for call in calls)


def test_release_workflows_stage_then_finalize_once() -> None:
    root = Path(__file__).resolve().parents[1]
    publish = (root / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    notes = (root / ".github/workflows/release_ai.yml").read_text(encoding="utf-8")

    assert "python release-automation/scripts/github_release.py ensure-draft" in publish
    assert "path: release-automation" in publish
    assert "ref: ${{ needs.resolve-tag.outputs.workflow_sha }}" in publish
    assert publish.index("- name: Build Python distributions") < publish.index(
        "- name: Checkout release automation"
    )
    assert "finalize-github-release:" in publish
    assert "python scripts/github_release.py wait-notes" in publish
    assert "--run-sha \"${{ needs.resolve-tag.outputs.workflow_sha }}\"" in publish
    assert "--source-sha \"${{ needs.resolve-tag.outputs.source_sha }}\"" in publish
    assert "python scripts/github_release.py finalize" in publish
    memory_verify = publish.split("  verify-avibe-memory-release:", 1)[1].split(
        "  publish-avibe-os:", 1
    )[0]
    avibe_publish = publish.split("  publish-avibe-os:", 1)[1].split(
        "  publish-vibe-remote:", 1
    )[0]
    legacy_publish = publish.split("  publish-vibe-remote:", 1)[1].split(
        "  finalize-github-release:", 1
    )[0]
    assert "publish-avibe-memory:" not in publish
    assert "environment: pypi-avibe-memory" not in publish
    assert "- finalize-github-release" in memory_verify
    assert "- verify-avibe-memory-release" in avibe_publish
    assert "- finalize-github-release" in avibe_publish
    assert "- finalize-github-release" in legacy_publish

    official_step = notes.split("- name: Update official release notes", 1)[1]
    assert "python scripts/github_release.py update-notes" in official_step
    assert "python scripts/github_release.py finalize" not in official_step
    assert github_release.NOTES_READY_MARKER_PREFIX in notes
    assert "run=${GITHUB_RUN_ID}" in notes
    assert 'LATEST_MODE="auto"' in publish
    assert 'elif [ "${{ github.event_name }}" = "push" ]' not in publish
    assert "<!-- avibe:update-notification=none -->" in notes
    assert "<!-- vibe-remote:update-notification=none -->" in notes
