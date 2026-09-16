from __future__ import annotations

from pathlib import Path

import pytest
from packaging.version import Version

from scripts.release_package_version import package_version_from_release_tag


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
        assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_MEMORY=$PACKAGE_VERSION" in workflow
