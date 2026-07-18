import json

from core import update_checker
from vibe import api


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


def test_packaged_install_keeps_semantic_update_behavior(monkeypatch) -> None:
    expected = {"current": "3.0.6", "latest": "3.0.7", "has_update": True, "error": None}
    monkeypatch.delenv("VIBE_BUILD_METADATA_PATH", raising=False)
    monkeypatch.setattr("vibe.__version__", "3.0.6", raising=False)
    monkeypatch.setattr(api, "get_latest_version_info", lambda current: expected if current == "3.0.6" else None)

    assert api.get_version_info() == {**expected, "build": {"kind": "package"}}


def test_configured_missing_metadata_stays_a_source_build(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VIBE_BUILD_METADATA_PATH", str(tmp_path / "not-written-yet.json"))
    monkeypatch.setattr("vibe.__version__", "3.0.6rc5", raising=False)
    monkeypatch.setattr(api, "get_latest_version_info", lambda _current: (_ for _ in ()).throw(AssertionError("source builds must not query PyPI")))

    assert api.get_version_info()["build"] == {"kind": "source"}
