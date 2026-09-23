from __future__ import annotations

from pathlib import Path

import pytest
from packaging.version import Version

from scripts.release_package_version import (
    latest_official_release,
    official_stable_version_key,
    package_version_from_release_tag,
)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v3.0.9", "3.0.9"),
        ("v3.0.10rc1", "3.0.10rc1"),
        ("gh-v3.0.10rc1", "3.0.10rc1"),
        ("gh-v4.1.0b2", "4.1.0b2"),
        ("v3.2.0rc1", "3.2.0rc1"),
        ("gh-v3.2.0-rc1", "3.2.0rc1"),
        ("gh-v3.2.0.rc01", "3.2.0rc1"),
        ("gh-v03.02.00-dev01", "3.2.0.dev1"),
        ("v3.2.0.post1", "3.2.0.post1"),
        ("gh-v3.2.0-a01.post02", "3.2.0a1.post2"),
    ],
)
def test_package_version_from_release_tag(tag: str, expected: str) -> None:
    assert package_version_from_release_tag(tag) == expected


@pytest.mark.parametrize("prefix", ["v", "gh-v"])
@pytest.mark.parametrize("release", ["3.1.0", "03.02.001"])
@pytest.mark.parametrize("phase", ["a", "b", "rc", "dev"])
@pytest.mark.parametrize("separator", ["", ".", "-"])
@pytest.mark.parametrize("number", ["0", "01", "12"])
def test_supported_tag_grammar_matches_build_backend_normalization(
    prefix, release, phase, separator, number,
) -> None:
    version = f"{release}{separator}{phase}{number}"
    for suffix in (["", ".post01"] if phase != "dev" else [""]):
        spelling = version + suffix
        canonical = str(Version(spelling))
        if prefix == "v" and spelling != canonical:
            with pytest.raises(ValueError, match="canonical spelling"):
                package_version_from_release_tag(prefix + spelling)
        else:
            assert package_version_from_release_tag(prefix + spelling) == canonical


@pytest.mark.parametrize(
    "tag",
    ["", "3.0.10rc1", "release-v3.0.10", "gh-vnext", "v3.1.0dev1.post2", "v3.1.0+local"],
)
def test_package_version_from_release_tag_rejects_unsupported_tags(tag: str) -> None:
    with pytest.raises(ValueError):
        package_version_from_release_tag(tag)


def test_package_release_workflows_pin_scm_version_to_release_tag() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"

    for name in ("release_ai.yml", "publish.yml"):
        workflow = (workflows / name).read_text(encoding="utf-8")
        assert "python scripts/release_package_version.py" in workflow
        assert "SETUPTOOLS_SCM_PRETEND_VERSION=$PACKAGE_VERSION" in workflow
        assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS=$PACKAGE_VERSION" in workflow


@pytest.mark.parametrize("tag", [
    None, 310, "", "model-hub-engine-v7.2.149-1", "gh-v3.1.0",
    "v3.2.0rc1", "v3.2.0.dev1", "v3.2.0a1.post2", "v03.01.00",
    "v3.1.0+local", "v3.1.0 ", " v3.1.0",
])
def test_only_canonical_official_stable_versions_can_be_latest(tag):
    assert official_stable_version_key(tag) is None


def test_stable_release_order_matches_package_version_order():
    tags = ["v3.0.9", "v3.0.10", "v3.1.0", "v3.1.0.post0", "v3.1.0.post2", "v4.0.0"]
    assert sorted(reversed(tags), key=official_stable_version_key) == sorted(
        tags, key=lambda tag: Version(tag[1:]),
    )


def test_latest_official_release_ignores_repository_latest_and_publication_order():
    tags = ["v3.1.0.post1", "v3.1.0", "v3.0.14", "model-hub-engine-v99.0.0",
            "gh-v9.0.0", "v9.0.0rc1"]
    releases = [{"tag_name": tag, "draft": False, "prerelease": False} for tag in tags]
    releases += [
        {"tag_name": "v8.0.0", "draft": True, "prerelease": False},
        {"tag_name": "v7.0.0", "draft": False, "prerelease": True},
    ]
    assert latest_official_release(releases) == releases[0]
    assert latest_official_release(reversed(releases)) == releases[0]
    assert latest_official_release([]) is None


@pytest.mark.parametrize("releases", [[None], [{"tag_name": "v3.1.0"}]])
def test_latest_selection_rejects_malformed_release_evidence(releases):
    with pytest.raises(ValueError):
        latest_official_release(releases)
