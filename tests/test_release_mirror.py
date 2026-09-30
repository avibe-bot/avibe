from __future__ import annotations

import hashlib
import re
from dataclasses import replace

import pytest

from scripts import release_mirror as mirror
from tests.test_desktop_release import ROOT, workflow


REPOSITORY = "avibe-bot/avibe"


def github_release(
    tag, *, published_at, prerelease=False, draft=False, assets=("a.tgz",), repository=REPOSITORY
):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published_at,
        "assets": [
            {
                "name": name,
                "size": len(name),
                "digest": f"sha256:{hashlib.sha256(name.encode()).hexdigest()}",
                "content_type": "application/x-gtar",
                "browser_download_url": (
                    f"https://github.com/{repository}/releases/download/{tag}/{name.replace('+', '%2B')}"
                ),
            }
            for name in assets
        ],
    }


def releases(*items, repository=REPOSITORY):
    return mirror.parse_releases(repository, items, {item["tag_name"]: "0" * 40 for item in items})


def test_selection_keeps_every_full_release_and_only_the_newest_prereleases():
    parsed = releases(
        github_release("v1.0.0", published_at="2026-01-01T00:00:00Z"),
        github_release("gh-v1.0.1rc1", published_at="2026-01-02T00:00:00Z", prerelease=True),
        github_release("model-hub-engine-v1-1", published_at="2026-01-03T00:00:00Z"),
        github_release("gh-v1.0.1rc2", published_at="2026-01-04T00:00:00Z", prerelease=True),
        github_release("gh-v1.0.1rc3", published_at="2026-01-05T00:00:00Z", prerelease=True),
        github_release("gh-v1.0.1rc4", published_at="2026-01-06T00:00:00Z", prerelease=True, draft=True),
    )

    assert [release.tag for release in mirror.select(parsed, keep_prereleases=2)] == [
        "gh-v1.0.1rc3", "gh-v1.0.1rc2", "model-hub-engine-v1-1", "v1.0.0",
    ]


def test_reconcile_plan_trusts_only_objects_the_prior_index_verified():
    (release,) = releases(
        github_release(
            "v1.0.0", published_at="2026-01-01T00:00:00Z",
            assets=("absent.tgz", "indexed.tgz", "replaced.tgz", "truncated.tgz", "unindexed.tgz"),
        ),
    )
    prior = mirror.render_index(
        REPOSITORY, [replace(release, assets=tuple(a for a in release.assets if a.name != "unindexed.tgz"))]
    )
    objects = {release.key(asset): asset.size for asset in release.assets if asset.name != "absent.tgz"}
    objects["releases/v1.0.0/truncated.tgz"] -= 1
    objects["releases/gh-v0.9.0rc1/old.tgz"] = 1
    objects[mirror.index_key(REPOSITORY)] = len(prior)
    objects["thirdparty/uv/uv.tar.gz"] = 1
    objects["askill/releases/v0.1.0/askill-linux-x64"] = 1
    current = replace(
        release,
        assets=tuple(
            replace(asset, sha256="f" * 64) if asset.name == "replaced.tgz" else asset
            for asset in release.assets
        ),
    )

    result = mirror.plan(REPOSITORY, [current], objects, prior)

    assert [key for key, _ in result.uploads] == [
        "releases/v1.0.0/absent.tgz",
        "releases/v1.0.0/replaced.tgz",
        "releases/v1.0.0/truncated.tgz",
        "releases/v1.0.0/unindexed.tgz",
    ]
    assert result.changed == ("releases/v1.0.0/replaced.tgz",)
    assert result.deletions == ("releases/gh-v0.9.0rc1/old.tgz",)


def test_sibling_repositories_reconcile_only_their_own_root():
    repository = "avibe-bot/askill"
    (release,) = releases(
        github_release("v0.1.15", published_at="2026-01-01T00:00:00Z", assets=("askill-linux-x64",),
                       repository=repository),
        repository=repository,
    )
    objects = {
        "askill/releases/v0.1.14/askill-linux-x64": 1,
        "avault/releases/v0.1.6/manifest.json": 1,
        "releases/v1.0.0/a.tgz": 1,
        "index/releases.json": 1,
    }

    result = mirror.plan(repository, [release], objects, None)

    assert [key for key, _ in result.uploads] == ["askill/releases/v0.1.15/askill-linux-x64"]
    assert result.deletions == ("askill/releases/v0.1.14/askill-linux-x64",)
    assert mirror.index_key(repository) == "askill/index/releases.json"


def test_a_failing_repository_does_not_stop_the_others_but_fails_the_run(monkeypatch, capsys):
    reconciled = []

    def reconcile(repository, bucket, *, keep_prereleases, dry_run):
        reconciled.append(repository)
        if repository == "avibe-bot/askill":
            raise mirror.MirrorError("gh api failed: HTTP 403")
        return 0

    monkeypatch.setattr(mirror, "reconcile", reconcile)

    assert mirror.main(["--bucket", "avibe", "--endpoint-url", "https://r2.invalid"]) == 1
    assert reconciled == list(mirror.REPOSITORIES)
    assert "error: avibe-bot/askill: gh api failed: HTTP 403" in capsys.readouterr().err


def test_tmux_builds_mirrors_its_full_releases_but_not_its_rebuilt_preview(monkeypatch):
    repository = "tmux/tmux-builds"
    items = [
        github_release("v3.6b", published_at="2026-05-20T11:54:47Z", assets=("tmux-3.6b-linux-x86_64.tar.gz",),
                       repository=repository),
        github_release("preview", published_at="2026-09-29T11:17:16Z", prerelease=True,
                       assets=("LICENSES.tar.gz",), repository=repository),
    ]
    bucket = RecordingBucket({}, None)
    monkeypatch.setattr(mirror, "Bucket", lambda name, endpoint_url: bucket)
    monkeypatch.setattr(mirror, "github_releases", lambda repository: items)
    monkeypatch.setattr(mirror, "tag_commits", lambda repository: {"v3.6b": "0" * 40, "preview": "1" * 40})
    monkeypatch.setattr(mirror, "download", lambda asset, destination: destination.write_bytes(b""))

    arguments = ["--repository", repository, "--bucket", "avibe", "--endpoint-url", "https://r2.invalid"]
    assert mirror.main(arguments) == 0
    assert bucket.writes == ["tmux/releases/v3.6b/tmux-3.6b-linux-x86_64.tar.gz", "tmux/index/releases.json"]


def test_a_completed_reconcile_is_a_no_op_on_the_next_run():
    selected = releases(
        github_release("v1.0.0", published_at="2026-01-01T00:00:00Z", assets=("a.tgz", "b+local.whl")),
        github_release("gh-v1.0.1rc1", published_at="2026-01-02T00:00:00Z", prerelease=True),
    )
    first = mirror.plan(REPOSITORY, selected, {}, None)
    objects = {key: asset.size for key, asset in first.uploads}

    again = mirror.plan(REPOSITORY, selected, objects, first.index)

    assert (again.uploads, again.deletions, again.changed) == ((), (), ())
    assert again.index == first.index


class RecordingBucket:
    def __init__(self, objects, index):
        self._objects, self._index, self.writes = objects, index, []

    def objects(self):
        return dict(self._objects)

    def read(self, key):
        return self._index

    def put(self, key, path, **metadata):
        self.writes.append(key)

    def delete(self, key):
        self.writes.append(key)


@pytest.mark.parametrize("dry_run", [True, False])
def test_published_bytes_changing_upstream_fails_the_run_even_as_a_dry_run(monkeypatch, dry_run):
    item = github_release("v1.0.0", published_at="2026-01-01T00:00:00Z")
    (release,) = releases(item)
    (asset,) = release.assets
    prior = mirror.render_index(REPOSITORY, [replace(release, assets=(replace(asset, sha256="f" * 64),))])
    bucket = RecordingBucket({release.key(asset): asset.size, "index/releases.json": len(prior)}, prior)
    monkeypatch.setattr(mirror, "github_releases", lambda repository: [item])
    monkeypatch.setattr(mirror, "tag_commits", lambda repository: {"v1.0.0": "0" * 40})
    monkeypatch.setattr(mirror, "download", lambda asset, destination: destination.write_bytes(b""))

    assert mirror.reconcile(REPOSITORY, bucket, keep_prereleases=20, dry_run=dry_run) == 1
    assert bucket.writes == ([] if dry_run else ["releases/v1.0.0/a.tgz", "index/releases.json"])


def test_an_empty_release_listing_never_plans_deleting_the_mirror():
    with pytest.raises(mirror.MirrorError, match="no published releases"):
        mirror.plan(REPOSITORY, [], {"releases/v1.0.0/a.tgz": 1}, None)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda asset: asset.update(digest=None), "no sha256 digest"),
        (lambda asset: asset.update(digest="md5:" + "0" * 32), "no sha256 digest"),
        (
            lambda asset: asset.update(browser_download_url="https://objects.example/a.tgz"),
            "unexpected download URL",
        ),
    ],
)
def test_assets_outside_the_mirror_contract_fail_closed(mutate, message):
    item = github_release("v1.0.0", published_at="2026-01-01T00:00:00Z")
    mutate(item["assets"][0])

    with pytest.raises(mirror.MirrorError, match=message):
        releases(item)


def test_download_refuses_bytes_that_differ_from_the_github_digest(tmp_path):
    source = tmp_path / "a.tgz"
    source.write_bytes(b"tampered")
    asset = mirror.Asset(
        name="a.tgz",
        size=len(b"tampered"),
        sha256=hashlib.sha256(b"original").hexdigest(),
        url=source.as_uri(),
        content_type="application/x-gtar",
    )

    with pytest.raises(mirror.MirrorError, match="do not match"):
        mirror.download(asset, tmp_path / "download")

    mirror.download(replace(asset, sha256=hashlib.sha256(b"tampered").hexdigest()), tmp_path / "download")
    assert (tmp_path / "download").read_bytes() == b"tampered"


def test_every_publishing_workflow_named_by_the_mirror_trigger_exists():
    names = {
        re.search(r"^name:\s*(.+)$", path.read_text(), re.MULTILINE).group(1).strip()
        for path in (ROOT / ".github/workflows").glob("*.yml")
    }
    triggers = workflow("release-mirror.yml")[True]["workflow_run"]["workflows"]

    assert triggers and set(triggers) <= names
