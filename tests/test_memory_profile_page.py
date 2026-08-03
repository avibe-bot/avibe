from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat

import pytest

from core.memory.profile_page import (
    MemoryProfilePageStore,
    PROFILE_PAGE_MAX_CSS_BYTES,
    PROFILE_PAGE_MAX_HTML_BYTES,
    ProfilePageStoreError,
    ProfilePageValidationError,
)
from core.memory.types import MemoryProfilePageSource


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "p-22222222222222222222222222222222"
GENERATED_AT = "2026-08-03T05:12:30Z"
SOURCE_UPDATED_AT = "2026-08-02T10:30:00Z"
SOURCE_SNAPSHOT = "sha256:" + "a" * 64


def _source(*, title: str = "How you work", extra: str = "") -> MemoryProfilePageSource:
    return MemoryProfilePageSource(
        index_html=f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <link rel="stylesheet" href="./styles.css">
  </head>
  <body>
    <main data-avibe-memory-profile-page="1">
      <header>
        <p>Generated <time data-avibe-generated-at datetime="{GENERATED_AT}">{GENERATED_AT}</time></p>
        <p>Profile <time data-avibe-source-updated-at datetime="{SOURCE_UPDATED_AT}">{SOURCE_UPDATED_AT}</time></p>
        <h1>{title}</h1>
      </header>
      <section><h2>Working style</h2><p>Prefers clear technical updates.</p></section>
      {extra}
    </main>
  </body>
</html>""",
        styles_css=""":root { color-scheme: light; font-family: system-ui, sans-serif; }
body { margin: 0; color: #17201b; background: #f7f8f5; }
main { max-width: 60rem; margin: auto; padding: 2rem; }
@media (max-width: 40rem) { main { padding: 1rem; } }""",
    )


def _publish(store: MemoryProfilePageStore, source: MemoryProfilePageSource):
    return store.publish(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        source=source,
        generated_at=GENERATED_AT,
        source_profile_updated_at=SOURCE_UPDATED_AT,
        source_profile_snapshot_id=SOURCE_SNAPSHOT,
    )


def test_profile_page_publish_restores_the_current_local_workspace(tmp_path: Path) -> None:
    store = MemoryProfilePageStore(tmp_path / "profile-pages")

    published = _publish(store, _source())
    restored = store.current(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
    )

    assert restored == published
    assert published.generated_at == GENERATED_AT
    assert published.source_profile_updated_at == SOURCE_UPDATED_AT
    assert published.source_profile_snapshot_id == SOURCE_SNAPSHOT
    assert store.read(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        artifact_id=published.artifact_id,
        asset_name="index.html",
    ).startswith(b"<!doctype html>")
    assert b"font-family" in store.read(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        artifact_id=published.artifact_id,
        asset_name="styles.css",
    )
    assert not (tmp_path / "show").exists()
    all_paths = tuple((tmp_path / "profile-pages").rglob("*"))
    assert all(PRINCIPAL not in str(path) and PROJECT not in str(path) for path in all_paths)
    for path in all_paths:
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600)


def test_profile_page_rejects_active_or_remote_content_and_keeps_last_good(tmp_path: Path) -> None:
    store = MemoryProfilePageStore(tmp_path / "profile-pages")
    first = _publish(store, _source(title="First"))

    invalid_sources = (
        _source(extra="<script>alert(1)</script>"),
        _source(extra='<img src="https://tracker.example/pixel.png">'),
        _source(extra='<div onclick="alert(1)">Click</div>'),
        MemoryProfilePageSource(
            index_html=_source().index_html,
            styles_css='@import url("https://fonts.example/style.css");',
        ),
        MemoryProfilePageSource(
            index_html=_source().index_html.replace(GENERATED_AT, "2026-08-03T05:12:31Z"),
            styles_css=_source().styles_css,
        ),
    )
    for source in invalid_sources:
        with pytest.raises(ProfilePageValidationError):
            _publish(store, source)

    assert store.current(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
    ) == first


@pytest.mark.parametrize(
    "mutate",
    (
        lambda html: "\n" + html,
        lambda html: html.replace(
            "<head>",
            '<head><meta http-equiv="content-security-policy" content="style-src none">',
        ),
        lambda html: html.replace(
            '<main data-avibe-memory-profile-page="1">',
            '<p>outside main</p><main data-avibe-memory-profile-page="1">',
        ),
        lambda html: html.replace(
            "<section>",
            '<svg><animate attributeName="href" values="https://tracker.example"></animate></svg><section>',
        ),
        lambda html: html.replace("</head>", "<?xml-stylesheet href='https://tracker.example'?></head>"),
        lambda html: html.replace("<header>", "<![CDATA[unexpected]]><header>"),
        lambda html: html.replace("</time>", "", 1),
        lambda html: html.replace("<!doctype html>", "<!doctype html>outside"),
        lambda html: html.replace(
            "<time data-avibe-generated-at",
            "<time hidden data-avibe-generated-at",
        ),
    ),
)
def test_profile_page_rejects_malformed_or_policy_overriding_html(
    tmp_path: Path,
    mutate,
) -> None:
    store = MemoryProfilePageStore(tmp_path / "profile-pages")
    valid = _source()

    with pytest.raises(ProfilePageValidationError):
        _publish(
            store,
            MemoryProfilePageSource(
                index_html=mutate(valid.index_html),
                styles_css=valid.styles_css,
            ),
        )


@pytest.mark.parametrize(
    "styles_css",
    (
        r"body { background: u\72l(https://tracker.example/pixel); }",
        "body { background: u/**/rl(/pixel); }",
        'body { background: image-set("https://tracker.example/pixel.png" 1x); }',
        "body { behavior: url(canary.htc); }",
    ),
)
def test_profile_page_rejects_obfuscated_or_alternate_css_fetches(
    tmp_path: Path,
    styles_css: str,
) -> None:
    source = _source()

    with pytest.raises(ProfilePageValidationError):
        _publish(
            MemoryProfilePageStore(tmp_path / "profile-pages"),
            MemoryProfilePageSource(index_html=source.index_html, styles_css=styles_css),
        )


def test_profile_page_accepts_a_distinct_static_svg_layout(tmp_path: Path) -> None:
    source = _source(
        extra=(
            '<svg viewBox="0 0 100 20" role="img" aria-label="Working rhythm">'
            '<rect x="0" y="2" width="70" height="16"></rect>'
            '<circle cx="82" cy="10" r="8"></circle>'
            "</svg><table><tbody><tr><th>Mode</th><td>Focused</td></tr></tbody></table>"
        )
    )

    published = _publish(MemoryProfilePageStore(tmp_path / "profile-pages"), source)

    assert published.artifact_id


def test_profile_page_accepts_an_absent_source_timestamp(tmp_path: Path) -> None:
    source = _source()
    source_marker = (
        f'<p>Profile <time data-avibe-source-updated-at datetime="{SOURCE_UPDATED_AT}">'
        f"{SOURCE_UPDATED_AT}</time></p>"
    )

    published = MemoryProfilePageStore(tmp_path / "profile-pages").publish(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        source=MemoryProfilePageSource(
            index_html=source.index_html.replace(source_marker, ""),
            styles_css=source.styles_css,
        ),
        generated_at=GENERATED_AT,
        source_profile_updated_at=None,
        source_profile_snapshot_id=SOURCE_SNAPSHOT,
    )

    assert published.source_profile_updated_at is None


@pytest.mark.parametrize(
    "source",
    (
        MemoryProfilePageSource(
            index_html=_source().index_html + "x" * PROFILE_PAGE_MAX_HTML_BYTES,
            styles_css=_source().styles_css,
        ),
        MemoryProfilePageSource(
            index_html=_source().index_html,
            styles_css="x" * (PROFILE_PAGE_MAX_CSS_BYTES + 1),
        ),
    ),
)
def test_profile_page_rejects_assets_over_their_byte_caps(
    tmp_path: Path,
    source: MemoryProfilePageSource,
) -> None:
    with pytest.raises(ProfilePageValidationError):
        _publish(MemoryProfilePageStore(tmp_path / "profile-pages"), source)


def test_profile_page_initial_publications_are_concurrency_safe_and_pruned(tmp_path: Path) -> None:
    root = tmp_path / "profile-pages"
    store = MemoryProfilePageStore(root)

    def publish(index: int):
        principal = f"u-{index + 1:032x}"
        return store.publish(
            scope_key=b"s" * 32,
            principal_id=principal,
            project_id=PROJECT,
            language="en",
            source=_source(title=f"Profile {index}"),
            generated_at=GENERATED_AT,
            source_profile_updated_at=SOURCE_UPDATED_AT,
            source_profile_snapshot_id=SOURCE_SNAPSHOT,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        published = tuple(executor.map(publish, range(8)))

    assert len({page.artifact_id for page in published}) == 8

    revisions = [_publish(store, _source(title=f"Revision {index}")) for index in range(4)]
    versions = next(root.rglob(revisions[-1].artifact_id)).parent
    stale_pointer = versions.parent / ".current-crash.tmp"
    stale_pointer.write_text("stale", encoding="utf-8")
    stale_pointer.chmod(0o600)
    os.utime(stale_pointer, (1, 1))
    stale_version = versions / ".tmp-crash"
    stale_version.mkdir(mode=0o700)
    os.utime(stale_version, (1, 1))
    revisions.append(_publish(store, _source(title="Revision 4")))
    version_names = {path.name for path in versions.iterdir()}
    assert len([name for name in version_names if len(name) == 32]) == 3
    assert stale_pointer.exists() is False
    assert stale_version.exists() is False
    assert store.current(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
    ) == revisions[-1]


def test_profile_page_same_scope_publications_keep_current_readable(tmp_path: Path) -> None:
    root = tmp_path / "profile-pages"
    store = MemoryProfilePageStore(root)

    def publish(index: int):
        return _publish(store, _source(title=f"Concurrent revision {index}"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        published = tuple(executor.map(publish, range(12)))

    current = store.current(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
    )
    assert current in published
    assert store.read(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        artifact_id=current.artifact_id,
        asset_name="index.html",
    ) is not None
    versions = next(root.rglob(current.artifact_id)).parent
    assert len([path for path in versions.iterdir() if len(path.name) == 32]) == 3


def test_profile_page_current_fails_closed_when_an_asset_is_modified(tmp_path: Path) -> None:
    root = tmp_path / "profile-pages"
    store = MemoryProfilePageStore(root)
    published = _publish(store, _source())
    index_path = next(root.rglob(f"{published.artifact_id}/index.html"))
    index_path.write_text("<!doctype html><title>tampered</title>", encoding="utf-8")

    assert store.current(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
    ) is None
    assert store.read(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        artifact_id=published.artifact_id,
        asset_name="index.html",
    ) is None


def test_profile_page_pointer_failure_preserves_current_and_cleans_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "profile-pages"
    store = MemoryProfilePageStore(root)
    first = _publish(store, _source(title="First"))
    real_replace = os.replace

    def fail_pointer_replace(source, destination) -> None:
        if Path(destination).name == "current.json":
            raise OSError("pointer-replace-canary")
        real_replace(source, destination)

    monkeypatch.setattr("core.memory.profile_page.os.replace", fail_pointer_replace)

    with pytest.raises(ProfilePageStoreError):
        _publish(store, _source(title="Second"))

    assert store.current(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
    ) == first
    assert tuple(root.rglob(".current-*.tmp")) == ()
    assert len(tuple(root.rglob("versions/[0-9a-f]*"))) == 1


def test_profile_page_rejects_a_symlinked_artifact_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "profile-pages"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises((ProfilePageValidationError, OSError)):
        _publish(MemoryProfilePageStore(root), _source())

    assert tuple(outside.iterdir()) == ()


def test_profile_page_reads_do_not_follow_a_replaced_version_directory(tmp_path: Path) -> None:
    root = tmp_path / "profile-pages"
    store = MemoryProfilePageStore(root)
    published = _publish(store, _source())
    version = next(path for path in root.rglob(published.artifact_id) if path.is_dir())
    backup = version.with_name(f"{published.artifact_id}-backup")
    version.rename(backup)
    version.symlink_to(backup, target_is_directory=True)

    assert store.current(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
    ) is None
    assert store.read(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        artifact_id=published.artifact_id,
        asset_name="index.html",
    ) is None


def test_profile_page_scopes_and_languages_do_not_collide(tmp_path: Path) -> None:
    store = MemoryProfilePageStore(tmp_path / "profile-pages")
    english = _publish(store, _source(title="English"))
    chinese_source = MemoryProfilePageSource(
        index_html=_source(title="Chinese").index_html.replace('lang="en"', 'lang="zh"'),
        styles_css=_source().styles_css,
    )
    chinese = store.publish(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="zh",
        source=chinese_source,
        generated_at=GENERATED_AT,
        source_profile_updated_at=SOURCE_UPDATED_AT,
        source_profile_snapshot_id=SOURCE_SNAPSHOT,
    )
    other_user = store.publish(
        scope_key=b"s" * 32,
        principal_id="u-33333333333333333333333333333333",
        project_id=PROJECT,
        language="en",
        source=_source(title="Other"),
        generated_at=GENERATED_AT,
        source_profile_updated_at=SOURCE_UPDATED_AT,
        source_profile_snapshot_id=SOURCE_SNAPSHOT,
    )

    assert len({english.artifact_id, chinese.artifact_id, other_user.artifact_id}) == 3
    assert store.read(
        scope_key=b"s" * 32,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        language="en",
        artifact_id=other_user.artifact_id,
        asset_name="index.html",
    ) is None


def test_profile_page_clear_removes_all_derived_pages(tmp_path: Path) -> None:
    root = tmp_path / "profile-pages"
    store = MemoryProfilePageStore(root)
    _publish(store, _source())

    store.clear_all()

    assert not root.exists()
