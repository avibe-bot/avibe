from __future__ import annotations

import errno
from pathlib import Path

import pytest

from core.show_pages import ensure_show_page_dir, show_page_dir
from core.show_router import default_show_router


LEGACY_ROUTER = Path(__file__).parent / "fixtures" / "show_pages" / "router-history-pre-ssr.tsx"


def test_fresh_router_comes_from_the_packaged_runtime_template():
    page = ensure_show_page_dir("sesfresh")
    assert (page / "src" / "router.tsx").read_text() == default_show_router()
    assert "export function SsrRouterProvider" in default_show_router()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "windows-crlf"])
def test_ensure_never_rewrites_the_old_router(newline):
    page = ensure_show_page_dir("sesupgrade")
    router = page / "src" / "router.tsx"
    original = LEGACY_ROUTER.read_bytes().replace(b"\n", newline)
    router.write_bytes(original)
    router.chmod(0o640)
    before = router.stat()
    others = {
        path.relative_to(page): path.read_bytes()
        for path in page.rglob("*")
        if path.is_file() and path != router
    }

    assert ensure_show_page_dir("sesupgrade") == page
    assert router.stat() == before
    assert router.read_bytes() == original
    ensure_show_page_dir("sesupgrade")
    assert router.stat().st_mtime_ns == before.st_mtime_ns
    assert router.stat().st_ino == before.st_ino
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
    assert ensure_show_page_dir("sescustom") == page
    assert router.read_bytes() == contents


def test_routerless_workspace_is_not_rewritten():
    page = show_page_dir("sesrouterless")
    (page / "src").mkdir(parents=True)
    (page / "src" / "App.tsx").write_text("export default function App() { return null }\n")
    ensure_show_page_dir("sesrouterless")
    assert not (page / "src" / "router.tsx").exists()


@pytest.mark.parametrize("linked_component", ["router", "src", "workspace"])
def test_initialization_does_not_rewrite_symlink_routers(tmp_path, linked_component):
    page = show_page_dir("seslinked")
    page.parent.mkdir(parents=True, exist_ok=True)
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
    ensure_show_page_dir("seslinked")
    assert target.read_bytes() == LEGACY_ROUTER.read_bytes()


def test_concurrent_edit_is_preserved(monkeypatch):
    page = ensure_show_page_dir("sesediting")
    router = page / "src" / "router.tsx"
    router.write_bytes(LEGACY_ROUTER.read_bytes())
    authored = b"// Editor saved while migration was preparing\n" + LEGACY_ROUTER.read_bytes()

    from core import show_pages
    write_defaults = show_pages._write_default_runtime_files

    def defaults_during_edit(*args):
        write_defaults(*args)
        router.write_bytes(authored)

    monkeypatch.setattr(show_pages, "_write_default_runtime_files", defaults_during_edit)
    ensure_show_page_dir("sesediting")
    assert router.read_bytes() == authored


def test_fresh_scaffold_does_not_replace_a_concurrently_created_router(monkeypatch):
    page = show_page_dir("sescreation")
    router = page / "src" / "router.tsx"
    authored = b"// Created by the editor during initialization\n"
    original_open = Path.open

    def create_before_open(path, mode="r", *args, **kwargs):
        if path == router and mode == "x":
            with original_open(path, "wb") as handle:
                handle.write(authored)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", create_before_open)
    ensure_show_page_dir("sescreation")
    assert router.read_bytes() == authored


@pytest.mark.parametrize("error", [PermissionError("fixture permission"), OSError(errno.EROFS, "fixture read-only mount")])
def test_existing_router_is_not_opened_for_migration(monkeypatch, error):
    page = ensure_show_page_dir("sesreadonly")
    router = page / "src" / "router.tsx"
    router.write_bytes(LEGACY_ROUTER.read_bytes())

    original_open = Path.open

    def readonly_router(path, *args, **kwargs):
        if path == router:
            raise error
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", readonly_router)
    assert ensure_show_page_dir("sesreadonly") == page
