import json
from pathlib import Path

import pytest

from core import update_checker
from vibe import api

# What the desktop updater writes for an available release; its own tests write
# the same document (desktop/runtime-host/src/update_status.rs).
DESKTOP_UPDATE_STATUS_FIXTURE = Path(__file__).parent / "fixtures" / "desktop_update_status_available.json"


def _write_metadata(path, *, commit: str, fingerprint: str = "same") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit": commit,
                "dirty": False,
                "fingerprints": {"python": fingerprint},
            }
        ),
        encoding="utf-8",
    )


def test_source_sync_identity_refreshes_without_package_reinstall(monkeypatch, tmp_path) -> None:
    metadata_path = tmp_path / "metadata.json"
    first_revision = "1" * 40
    second_revision = "2" * 40
    monkeypatch.setenv("VIBE_BUILD_METADATA_PATH", str(metadata_path))
    monkeypatch.setattr("vibe.__version__", "3.0.6rc5", raising=False)
    monkeypatch.setattr(api, "get_latest_version_info", lambda _current: (_ for _ in ()).throw(AssertionError("source builds must not query PyPI")))

    _write_metadata(metadata_path, commit=first_revision)
    first = api.get_version_info()
    _write_metadata(metadata_path, commit=second_revision)
    second = api.get_version_info()

    assert first == {
        "current": "3.0.6rc5",
        "latest": None,
        "has_update": False,
        "error": None,
        "build": {"kind": "source", "revision": first_revision, "dirty": False},
    }
    assert second["current"] == first["current"]
    assert second["build"] == {"kind": "source", "revision": second_revision, "dirty": False}


def test_source_build_skips_background_package_update_check(monkeypatch, tmp_path) -> None:
    metadata_path = tmp_path / "metadata.json"
    _write_metadata(metadata_path, commit="a" * 40)
    monkeypatch.setenv("VIBE_BUILD_METADATA_PATH", str(metadata_path))
    monkeypatch.setattr("vibe.__version__", "3.0.6rc5", raising=False)
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source builds must not query PyPI")),
    )

    assert update_checker._fetch_pypi_version_sync() == {
        "current": "3.0.6rc5",
        "latest": None,
        "has_update": False,
        "error": None,
    }


def test_desktop_managed_runtime_skips_background_package_update_check(monkeypatch) -> None:
    monkeypatch.delenv("VIBE_BUILD_METADATA_PATH", raising=False)
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setattr("vibe.__version__", "3.0.6", raising=False)
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("desktop Runtime must not query PyPI")),
    )

    assert update_checker._fetch_pypi_version_sync() == {
        "current": "3.0.6",
        "latest": None,
        "has_update": False,
        "error": None,
    }


def test_packaged_install_keeps_semantic_update_behavior(monkeypatch) -> None:
    expected = {"current": "3.0.6", "latest": "3.0.7", "has_update": True, "error": None}
    monkeypatch.delenv("VIBE_BUILD_METADATA_PATH", raising=False)
    monkeypatch.setattr("vibe.__version__", "3.0.6", raising=False)
    monkeypatch.setattr(api, "get_latest_version_info", lambda current: expected if current == "3.0.6" else None)

    assert api.get_version_info() == {**expected, "build": {"kind": "package"}}


def _desktop_managed(monkeypatch, state_dir: Path) -> None:
    monkeypatch.delenv("VIBE_BUILD_METADATA_PATH", raising=False)
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(state_dir))
    monkeypatch.setattr("vibe.__version__", "3.2.2rc13", raising=False)
    monkeypatch.setattr(
        api,
        "get_latest_version_info",
        lambda _current: (_ for _ in ()).throw(AssertionError("desktop Runtime must not query PyPI")),
    )


# A desktop app with no usable conclusion: the payload #2893 shipped.
DESKTOP_NO_CONCLUSION = {
    "current": "3.2.2rc13",
    "latest": None,
    "has_update": False,
    "error": None,
    "build": {"kind": "package"},
    "managed_by": "desktop",
}


def test_desktop_managed_runtime_without_desktop_status_skips_package_update_check(monkeypatch, tmp_path) -> None:
    _desktop_managed(monkeypatch, tmp_path)

    assert api.get_version_info() == DESKTOP_NO_CONCLUSION


def test_desktop_managed_runtime_relays_the_desktop_update_status(monkeypatch, tmp_path) -> None:
    _desktop_managed(monkeypatch, tmp_path)
    (tmp_path / "update-status.json").write_bytes(DESKTOP_UPDATE_STATUS_FIXTURE.read_bytes())

    assert api.get_version_info() == {**DESKTOP_NO_CONCLUSION, "latest": "3.2.3", "has_update": True}


def test_desktop_managed_runtime_relays_an_up_to_date_desktop(monkeypatch, tmp_path) -> None:
    _desktop_managed(monkeypatch, tmp_path)
    status = json.loads(DESKTOP_UPDATE_STATUS_FIXTURE.read_text(encoding="utf-8"))
    # A newer desktop app may add fields this Runtime does not know.
    status.update(phase="current", latest="3.2.2-rc.13", has_update=False, written_by_a_newer_app=True)
    (tmp_path / "update-status.json").write_text(json.dumps(status), encoding="utf-8")

    assert api.get_version_info() == {**DESKTOP_NO_CONCLUSION, "latest": "3.2.2-rc.13"}


def _fixture_with(**changes) -> str:
    status = json.loads(DESKTOP_UPDATE_STATUS_FIXTURE.read_text(encoding="utf-8"))
    status.update(changes)
    return json.dumps(status)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param('{"schema_version": 1, "phase": "avail', id="truncated"),
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param("[]", id="not-an-object"),
        pytest.param(_fixture_with(schema_version=2), id="future-schema"),
        pytest.param(_fixture_with(schema_version=True), id="boolean-schema"),
        pytest.param(_fixture_with(latest=None), id="update-without-latest"),
        pytest.param(_fixture_with(latest="  "), id="update-with-blank-latest"),
        pytest.param(_fixture_with(latest=323), id="non-string-latest"),
        pytest.param(_fixture_with(has_update="true"), id="non-boolean-has-update"),
        pytest.param(None, id="unreadable"),
    ],
)
def test_desktop_managed_runtime_treats_an_unusable_desktop_status_as_missing(monkeypatch, tmp_path, content) -> None:
    _desktop_managed(monkeypatch, tmp_path)
    target = tmp_path / "update-status.json"
    if content is None:
        target.mkdir()
    elif isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")

    # Never the PyPI failure path: an `error` would render "check failed".
    assert api.get_version_info() == DESKTOP_NO_CONCLUSION


def test_configured_missing_metadata_stays_a_source_build(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VIBE_BUILD_METADATA_PATH", str(tmp_path / "not-written-yet.json"))
    monkeypatch.setattr("vibe.__version__", "3.0.6rc5", raising=False)
    monkeypatch.setattr(api, "get_latest_version_info", lambda _current: (_ for _ in ()).throw(AssertionError("source builds must not query PyPI")))

    assert api.get_version_info()["build"] == {"kind": "source"}
