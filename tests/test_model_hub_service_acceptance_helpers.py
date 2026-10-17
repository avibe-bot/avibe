"""MH-SERVICE-001 accepts exactly the engine build its selected manifest names (#2780)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.e2e import test_model_hub_service_acceptance as acceptance

_ENV = "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH"
_PLATFORM = "linux-arm64"


def _payload(version: object = "v8.0.30", sha: str = "a" * 64, source_sha: str = "c" * 40) -> dict:
    return {
        "schema_version": 1,
        "version": version,
        "source_sha": source_sha,
        "assets": [{"platform": _PLATFORM, "url": "file:///x.tar.gz", "size_bytes": 10, "sha256": sha}],
    }


def _manifest(path: Path, **fields: object) -> Path:
    path.write_text(json.dumps(_payload(**fields)), encoding="utf-8")
    return path


def _runtime(loaded: dict, installed: str | None = None, *, verified: bool = True) -> dict:
    """The runtime status as the service reports it: the manifest it loaded and its install."""

    return {
        "host_platform": _PLATFORM,
        "manifest": {"name": "cliproxyapi", "resolution": "resolved", "version": loaded["version"],
                     "source_sha": loaded["source_sha"], "assets": loaded["assets"]},
        "status": {"installed_version": installed or loaded["version"], "verified": verified},
    }


def test_a_candidate_run_selects_the_candidate_with_no_other_input(tmp_path):
    packaged = _manifest(tmp_path / "packaged.json", version="v8.0.23")
    candidate = _manifest(tmp_path / "candidate.json", version="v8.0.30")

    selected = acceptance._selected_engine_manifest({_ENV: str(candidate)}, packaged)

    assert selected["version"] == "v8.0.30"
    acceptance._assert_installed_engine(_runtime(selected), selected)


def test_without_an_override_the_packaged_manifest_is_selected(tmp_path):
    packaged = _manifest(tmp_path / "packaged.json", version="v8.0.23")

    for env in ({}, {_ENV: ""}):
        selected = acceptance._selected_engine_manifest(env, packaged)
        assert selected["version"] == "v8.0.23"
        acceptance._assert_installed_engine(_runtime(selected), selected)


@pytest.mark.parametrize(
    "packaged_fields",
    [
        pytest.param({"version": "v8.0.23", "sha": "b" * 64}, id="another-version"),
        # A rebuild with other patches keeps the upstream version but not the bytes.
        pytest.param({"version": "v8.0.30", "sha": "b" * 64}, id="same-version-other-build"),
        pytest.param({"version": "v8.0.30", "source_sha": "d" * 40}, id="same-version-other-source"),
    ],
)
def test_an_ignored_override_that_installs_the_packaged_engine_fails_the_run(tmp_path, packaged_fields):
    selected = acceptance._selected_engine_manifest(
        {_ENV: str(_manifest(tmp_path / "candidate.json"))}, _manifest(tmp_path / "packaged.json")
    )
    packaged = _payload(**packaged_fields)

    with pytest.raises(AssertionError):
        acceptance._assert_installed_engine(_runtime(packaged), selected)


def test_an_engine_not_verified_against_the_selected_build_fails_the_run(tmp_path):
    selected = acceptance._selected_engine_manifest({}, _manifest(tmp_path / "packaged.json"))

    with pytest.raises(AssertionError):
        acceptance._assert_installed_engine(_runtime(selected, verified=False), selected)
    with pytest.raises(AssertionError, match="installed engine v8.0.23"):
        acceptance._assert_installed_engine(_runtime(selected, installed="v8.0.23"), selected)


def test_a_selected_manifest_without_an_asset_for_the_host_fails_the_run(tmp_path):
    selected = acceptance._selected_engine_manifest({}, _manifest(tmp_path / "packaged.json"))
    selected["assets"] = []

    with pytest.raises(pytest.fail.Exception, match="selected engine manifest has no asset"):
        acceptance._assert_installed_engine(_runtime(_payload()), selected)


@pytest.mark.parametrize(
    "selected",
    [
        pytest.param(lambda tmp: tmp / "absent.json", id="missing"),
        pytest.param(lambda tmp: _write(tmp / "bad.json", "{not json"), id="malformed"),
        pytest.param(lambda tmp: _write(tmp / "list.json", "[]"), id="not-an-object"),
        pytest.param(lambda tmp: _manifest(tmp / "none.json", version=None), id="no-version"),
        pytest.param(lambda tmp: _manifest(tmp / "blank.json", version="  "), id="blank-version"),
        pytest.param(lambda tmp: _manifest(tmp / "number.json", version=8), id="non-string-version"),
    ],
)
@pytest.mark.parametrize("which", ["candidate", "packaged"])
def test_a_selected_manifest_without_a_usable_version_fails_the_run(tmp_path, selected, which):
    path = selected(tmp_path)
    good = _manifest(tmp_path / "good.json")
    env, packaged = ({_ENV: str(path)}, good) if which == "candidate" else ({}, path)

    with pytest.raises(pytest.fail.Exception, match="selected engine manifest"):
        acceptance._selected_engine_manifest(env, packaged)


def test_a_relative_override_reaches_the_service_and_the_acceptance_as_one_absolute_file(tmp_path, monkeypatch):
    """The service resolves a relative path from its own working directory, so the acceptance hands it an absolute one."""

    candidate = _manifest(tmp_path / "candidate.json")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENV, "candidate.json")

    service = acceptance._Service(tmp_path / "home", 0)
    try:
        assert service.env[_ENV] == str(candidate.resolve())
        assert os.path.isabs(service.env[_ENV])
    finally:
        service.close()


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
