from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from core.show_pages import ensure_show_page_dir, show_page_dir
from core.show_router import _LEGACY_ROUTER_SHA256, default_show_router, upgrade_default_show_router


LEGACY_ROUTER = Path(__file__).parent / "fixtures" / "show_pages" / "router-history-pre-ssr.tsx"


def test_released_fixture_is_the_exact_migration_target():
    original = LEGACY_ROUTER.read_bytes()
    assert {
        hashlib.sha256(original).hexdigest(),
        hashlib.sha256(original.replace(b"\n", b"\r\n")).hexdigest(),
    } == _LEGACY_ROUTER_SHA256


def test_fresh_router_comes_from_the_packaged_runtime_template():
    page = ensure_show_page_dir("sesfresh")
    assert (page / "src" / "router.tsx").read_text() == default_show_router()
    assert "export function SsrRouterProvider" in default_show_router()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "windows-crlf"])
def test_ensure_upgrades_only_the_old_router_and_is_idempotent(newline):
    page = ensure_show_page_dir("sesupgrade")
    router = page / "src" / "router.tsx"
    router.write_bytes(LEGACY_ROUTER.read_bytes().replace(b"\n", newline))
    router.chmod(0o640)
    others = {
        path.relative_to(page): path.read_bytes()
        for path in page.rglob("*")
        if path.is_file() and path != router
    }

    assert ensure_show_page_dir("sesupgrade") == page
    assert router.read_text() == default_show_router()
    assert router.stat().st_mode & 0o777 == 0o640
    updated = router.stat()
    ensure_show_page_dir("sesupgrade")
    assert router.stat() == updated
    assert all((page / path).read_bytes() == content for path, content in others.items())


@pytest.mark.parametrize(
    "contents",
    [
        b"// Authored by the user\n" + LEGACY_ROUTER.read_bytes(),
        b'export function RouterView() { return <h1>Custom</h1> }\n',
        b'window.addEventListener("hashchange", () => {})\n',
        b"// Non-UTF8 custom source: \xff",
    ],
)
def test_unknown_routers_are_never_rewritten(contents):
    page = ensure_show_page_dir("sescustom")
    router = page / "src" / "router.tsx"
    router.write_bytes(contents)
    assert not upgrade_default_show_router(page)
    assert ensure_show_page_dir("sescustom") == page
    assert router.read_bytes() == contents


def test_routerless_workspace_is_not_migrated():
    page = show_page_dir("sesrouterless")
    (page / "src").mkdir(parents=True)
    (page / "src" / "App.tsx").write_text("export default function App() { return null }\n")
    assert not upgrade_default_show_router(page)
    ensure_show_page_dir("sesrouterless")
    assert not (page / "src" / "router.tsx").exists()


@pytest.mark.parametrize("linked_component", ["router", "src", "workspace"])
def test_migration_does_not_follow_symlinks(tmp_path, linked_component):
    page = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (outside / "src").mkdir(parents=True)
    target = outside / "src" / "router.tsx"
    target.write_bytes(LEGACY_ROUTER.read_bytes())
    if linked_component == "workspace":
        page.symlink_to(outside, target_is_directory=True)
    elif linked_component == "src":
        page.mkdir()
        (page / "src").symlink_to(outside / "src", target_is_directory=True)
    else:
        (page / "src").mkdir(parents=True)
        (page / "src" / "router.tsx").symlink_to(target)
    assert not upgrade_default_show_router(page)
    assert target.read_bytes() == LEGACY_ROUTER.read_bytes()


def test_concurrent_edit_is_preserved(monkeypatch):
    page = ensure_show_page_dir("sesediting")
    router = page / "src" / "router.tsx"
    router.write_bytes(LEGACY_ROUTER.read_bytes())
    authored = b"// Editor saved while migration was preparing\n" + LEGACY_ROUTER.read_bytes()

    def template_during_edit():
        router.write_bytes(authored)
        return default_show_router()

    monkeypatch.setattr("core.show_router.default_show_router", template_during_edit)
    assert not upgrade_default_show_router(page)
    assert router.read_bytes() == authored
    assert not list(router.parent.glob(".router-*.tmp"))


def test_failed_publish_preserves_old_router_and_cleans_temporary_file(monkeypatch):
    page = ensure_show_page_dir("sesfailure")
    router = page / "src" / "router.tsx"
    router.write_bytes(LEGACY_ROUTER.read_bytes())

    def fail_replace(*args):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="fixture disk failure"):
        upgrade_default_show_router(page)
    assert router.read_bytes() == LEGACY_ROUTER.read_bytes()
    assert not list(router.parent.glob(".router-*.tmp"))


def test_readonly_workspace_still_initializes_without_a_migration(monkeypatch, caplog):
    page = ensure_show_page_dir("sesreadonly")
    router = page / "src" / "router.tsx"
    router.write_bytes(LEGACY_ROUTER.read_bytes())

    def readonly_directory(*args, **kwargs):
        raise PermissionError("fixture read-only workspace")

    monkeypatch.setattr("core.show_router.tempfile.mkstemp", readonly_directory)
    assert ensure_show_page_dir("sesreadonly") == page
    assert router.read_bytes() == LEGACY_ROUTER.read_bytes()
    assert "read-only workspace" in caplog.text
