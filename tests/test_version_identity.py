import io
import json
from pathlib import Path

import pytest

from core import update_checker
from vibe import api, upgrade

# The index fixture the desktop updater's own tests select from.
DESKTOP_INDEX_FIXTURE = Path(__file__).resolve().parents[1] / "desktop/runtime-host/tests/fixtures/updater/releases-index.json"


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


def _serve_desktop_index(monkeypatch, body: bytes) -> list[str]:
    requested: list[str] = []

    def urlopen(req, timeout):
        requested.append(req.full_url)
        return io.BytesIO(body)

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(api, "get_latest_version_info", lambda _current: (_ for _ in ()).throw(AssertionError("desktop Runtime must not query PyPI")))
    return requested


@pytest.mark.parametrize(
    ("current", "latest"),
    [("3.1.2rc6", "3.1.2rc7"), ("3.1.0", "3.1.1")],
)
def test_desktop_managed_runtime_reports_updates_from_the_desktop_index(monkeypatch, current, latest) -> None:
    monkeypatch.delenv("VIBE_BUILD_METADATA_PATH", raising=False)
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setattr("vibe.__version__", current, raising=False)
    requested = _serve_desktop_index(monkeypatch, DESKTOP_INDEX_FIXTURE.read_bytes())

    assert api.get_version_info() == {
        "current": current,
        "latest": latest,
        "has_update": True,
        "error": None,
        "build": {"kind": "package"},
        "managed_by": "desktop",
    }
    assert requested == [upgrade.DESKTOP_RELEASE_INDEX_URL]


def test_desktop_managed_runtime_reports_an_unreadable_index_as_a_failed_check(monkeypatch) -> None:
    monkeypatch.delenv("VIBE_BUILD_METADATA_PATH", raising=False)
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setattr("vibe.__version__", "3.1.1", raising=False)
    _serve_desktop_index(monkeypatch, json.dumps({"schema_version": 2, "repository": "avibe-bot/avibe", "releases": []}).encode())

    result = api.get_version_info()
    assert (result["latest"], result["has_update"], result["managed_by"]) == (None, False, "desktop")
    assert result["error"] == "unsupported release index"


def test_desktop_channel_selection_matches_the_desktop_updater() -> None:
    # Mirrors desktop/runtime-host/tests/updater.rs so the two rule sets cannot drift.
    index = json.loads(DESKTOP_INDEX_FIXTURE.read_text(encoding="utf-8"))
    assert upgrade.select_latest_desktop_release(index, test=True) == "3.1.2rc7"
    assert upgrade.select_latest_desktop_release(index, test=False) == "3.1.1"

    listed = {
        "schema_version": 1,
        "repository": "avibe-bot/avibe",
        "releases": [
            {"tag": tag, "prerelease": prerelease}
            for tag, prerelease in [
                ("gh-v3.1.2rc9", True),
                ("gh-v3.1.2rc10", True),
                ("gh-v3.1.3rc01", True),
                ("gh-v9.0.0rc1", False),
                ("v9.0.0", True),
                ("v3.1.1", False),
                ("v03.2.0", False),
                ("v3.2.0-rc.1", False),
            ]
        ],
    }
    assert upgrade.select_latest_desktop_release(listed, test=True) == "3.1.2rc10"
    assert upgrade.select_latest_desktop_release(listed, test=False) == "3.1.1"
    assert upgrade.select_latest_desktop_release({**listed, "releases": []}, test=False) is None
    for unsupported in ({**listed, "schema_version": 2}, {**listed, "repository": "avibe-bot/other"}):
        with pytest.raises(ValueError):
            upgrade.select_latest_desktop_release(unsupported, test=True)


def test_desktop_managed_source_build_still_skips_package_update_check(monkeypatch, tmp_path) -> None:
    metadata_path = tmp_path / "metadata.json"
    _write_metadata(metadata_path, commit="b" * 40)
    monkeypatch.setenv("VIBE_BUILD_METADATA_PATH", str(metadata_path))
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setattr("vibe.__version__", "3.0.6rc5", raising=False)
    monkeypatch.setattr(api, "get_latest_version_info", lambda _current: (_ for _ in ()).throw(AssertionError("source builds must not query PyPI")))

    result = api.get_version_info()
    assert (result["latest"], result["has_update"], result["managed_by"]) == (None, False, "desktop")


def test_configured_missing_metadata_stays_a_source_build(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VIBE_BUILD_METADATA_PATH", str(tmp_path / "not-written-yet.json"))
    monkeypatch.setattr("vibe.__version__", "3.0.6rc5", raising=False)
    monkeypatch.setattr(api, "get_latest_version_info", lambda _current: (_ for _ in ()).throw(AssertionError("source builds must not query PyPI")))

    assert api.get_version_info()["build"] == {"kind": "source"}
