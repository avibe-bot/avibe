"""MH-SERVICE-001 accepts exactly the engine its selected manifest names (#2780)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e import test_model_hub_service_acceptance as acceptance

_ENV = "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH"


def _manifest(path: Path, version: object = "v8.0.30") -> Path:
    path.write_text(json.dumps({"schema_version": 1, "version": version, "assets": []}), encoding="utf-8")
    return path


def test_a_candidate_run_expects_the_candidate_version_with_no_other_input(tmp_path):
    packaged = _manifest(tmp_path / "packaged.json", "v8.0.23")
    candidate = _manifest(tmp_path / "candidate.json", "v8.0.30")

    assert acceptance._selected_engine_version({_ENV: str(candidate)}, packaged, cwd=tmp_path) == "v8.0.30"


def test_a_relative_candidate_path_resolves_from_the_service_directory(tmp_path):
    packaged = _manifest(tmp_path / "packaged.json", "v8.0.23")
    _manifest(tmp_path / "candidate.json", "v8.0.30")

    assert acceptance._selected_engine_version({_ENV: "candidate.json"}, packaged, cwd=tmp_path) == "v8.0.30"


def test_without_an_override_the_packaged_version_is_expected(tmp_path):
    packaged = _manifest(tmp_path / "packaged.json", "v8.0.23")

    for env in ({}, {_ENV: ""}):
        assert acceptance._selected_engine_version(env, packaged, cwd=tmp_path) == "v8.0.23"


def test_an_engine_installed_from_another_manifest_fails_the_run(tmp_path):
    """An ignored override installs the packaged engine; the run must fail, not pass as candidate acceptance."""

    packaged = _manifest(tmp_path / "packaged.json", "v8.0.23")
    candidate = _manifest(tmp_path / "candidate.json", "v8.0.30")
    expected = acceptance._selected_engine_version({_ENV: str(candidate)}, packaged, cwd=tmp_path)
    runtime = {"status": {"installed_version": "v8.0.23"}}

    with pytest.raises(AssertionError, match="v8.0.30"):
        acceptance._assert_installed_engine(runtime, expected)
    acceptance._assert_installed_engine({"status": {"installed_version": "v8.0.30"}}, expected)


@pytest.mark.parametrize(
    "selected",
    [
        pytest.param(lambda tmp: tmp / "absent.json", id="missing"),
        pytest.param(lambda tmp: _write(tmp / "bad.json", "{not json"), id="malformed"),
        pytest.param(lambda tmp: _write(tmp / "list.json", "[]"), id="not-an-object"),
        pytest.param(lambda tmp: _manifest(tmp / "none.json", None), id="no-version"),
        pytest.param(lambda tmp: _manifest(tmp / "blank.json", "  "), id="blank-version"),
        pytest.param(lambda tmp: _manifest(tmp / "number.json", 8), id="non-string-version"),
    ],
)
@pytest.mark.parametrize("which", ["candidate", "packaged"])
def test_a_selected_manifest_without_a_usable_version_fails_the_run(tmp_path, selected, which):
    path = selected(tmp_path)
    good = _manifest(tmp_path / "good.json", "v8.0.23")
    env, packaged = ({_ENV: str(path)}, good) if which == "candidate" else ({}, path)

    with pytest.raises(pytest.fail.Exception, match="selected engine manifest"):
        acceptance._selected_engine_version(env, packaged, cwd=tmp_path)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
