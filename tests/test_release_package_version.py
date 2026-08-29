from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release_package_version import package_version_from_release_tag


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v3.0.9", "3.0.9"),
        ("v3.0.10rc1", "3.0.10rc1"),
        ("gh-v3.0.10rc1", "3.0.10rc1"),
        ("gh-v4.1.0b2", "4.1.0b2"),
    ],
)
def test_package_version_from_release_tag(tag: str, expected: str) -> None:
    assert package_version_from_release_tag(tag) == expected


@pytest.mark.parametrize("tag", ["", "3.0.10rc1", "release-v3.0.10", "gh-vnext"])
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
