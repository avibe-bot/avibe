"""Unit tests for the workbench chat-media proxy spine.

Covers ``storage.media_service`` (token mint + readback, derived content-type /
ext / size) and ``core.workbench_media.rewrite_agent_media`` (in-place file://
rewrite, image vs file kind, external URLs untouched). Uses an isolated temp
SQLite migrated to head, so it never touches real ``~/.vibe_remote`` state.
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import patch

import pytest
from sqlalchemy import select

from core.workbench_media import MAX_WORKBENCH_ATTACHMENT_BYTES, rewrite_agent_media
from storage import media_service, settings_service
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, media_object_references, media_objects


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_workbench_attachment_limit_is_100_mib():
    assert MAX_WORKBENCH_ATTACHMENT_BYTES == 100 * 1024 * 1024


def _png_bytes(width: int, height: int) -> bytes:
    """A genuinely valid PNG of the given pixel size (stdlib only), so the
    dimension probe reads the real header instead of a hand-faked one."""

    def _chunk(typ: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    scanlines = (b"\x00" + b"\x00\x00\x00" * width) * height  # one filter byte + RGB per row
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(scanlines))
        + _chunk(b"IEND", b"")
    )


def _seed_scope_and_session(conn) -> str:
    scope_id = settings_service.upsert_scope(
        conn,
        platform="avibe",
        scope_type="project",
        native_id="proj-1",
        now=_now(),
        supports_threads=False,
    )
    conn.execute(
        agent_sessions.insert().values(
            id="sess_x",
            scope_id=scope_id,
            agent_backend="claude",
            agent_variant="default",
            session_anchor="anchor",
            native_session_id="native",
            status="active",
            metadata_json="{}",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return scope_id


def test_register_and_get_by_token(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        token = media_service.register(
            conn,
            scope_id=scope_id,
            session_id="sess_x",
            kind="image",
            source="agent_reply",
            local_path=str(shot),
            file_name="shot.png",
        )

    with engine.connect() as conn:
        row = media_service.get_by_token(conn, token)
        assert media_service.get_by_token(conn, "does-not-exist") is None

    assert row is not None
    assert row["kind"] == "image"
    assert row["source"] == "agent_reply"
    assert row["content_type"] == "image/png"
    assert row["file_ext"] == "png"
    assert row["size_bytes"] == 8
    assert row["mtime_ns"] is not None
    assert row["local_path"] == str(shot)
    assert row["session_id"] == "sess_x"


def test_rewrite_in_place_image_file_and_external(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    doc = tmp_path / "r.pdf"
    doc.write_bytes(b"y")

    text = (
        f"See ![chart](file://{img}) and the [report](file://{doc}); "
        f"also [docs](https://example.com/x)."
    )

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)

    # Both file:// links are rewritten in place to same-origin proxy URLs.
    assert "file://" not in out
    assert out.startswith("See ![chart](/api/media/")
    assert "the [report](/api/media/" in out
    # External URL is left untouched (no token, no rewrite).
    assert "[docs](https://example.com/x)" in out

    with engine.connect() as conn:
        rows = conn.execute(select(media_objects)).mappings().all()
    assert sorted(r["kind"] for r in rows) == ["file", "image"]
    assert all(r["source"] == "agent_reply" for r in rows)


@pytest.mark.parametrize("legacy", [False, True], ids=["new", "existing"])
@pytest.mark.parametrize("filename", ["report.pdf", "报告 (最终).docx", "data.v2.tar.gz", "README"])
def test_agent_download_and_metadata_preserve_real_filename(
    tmp_path, monkeypatch, filename, legacy, sqlite_schema_db_factory,
):
    from tests.ui_server_test_helpers import _save_config
    from vibe import ui_server

    _save_config(tmp_path)
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    monkeypatch.setattr(ui_server, "_projects_engine", lambda: engine)
    document = tmp_path / filename
    document.write_bytes(b"document contents")
    label = "下载报告 v2.1.pdf"
    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        rewritten = rewrite_agent_media(
            conn, scope_id=scope_id, session_id="sess_x", text=f"[{label}](<{document.as_uri()}>)"
        )
        row = dict(conn.execute(select(media_objects)).mappings().one())
        token = row["token"]
        assert rewritten == f"[{label}](</api/media/{token}>)"
        if legacy:
            conn.execute(media_objects.update().where(media_objects.c.token == token).values(file_name=label))
        else:
            assert row["file_name"] == filename

    client = ui_server.app.test_client()
    meta = client.get(f"/api/media/{token}/meta")
    assert meta.status_code == 200
    assert meta.get_json()["name"] == filename
    assert meta.get_json()["ext"] == row["file_ext"]
    for query in ("", "?download=1"):
        response = client.get(f"/api/media/{token}{query}")
        assert response.status_code == 200
        assert response.content == document.read_bytes()
        disposition = Message()
        disposition["Content-Disposition"] = response.headers["Content-Disposition"]
        assert disposition.get_filename() == filename
        assert response.headers["Content-Type"].split(";", 1)[0] == row["content_type"]
        if query:
            assert disposition.get_content_disposition() == "attachment"
    with engine.connect() as conn:
        stored_name = conn.execute(select(media_objects.c.file_name).where(media_objects.c.token == token)).scalar_one()
    assert stored_name == (label if legacy else filename)
    engine.dispose()


def test_uploaded_download_keeps_original_name_instead_of_storage_basename(tmp_path, monkeypatch, sqlite_schema_db_factory):
    from tests.ui_server_test_helpers import _save_config
    from vibe import ui_server

    _save_config(tmp_path)
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    monkeypatch.setattr(ui_server, "_projects_engine", lambda: engine)
    document = tmp_path / "random-upload-id_report.docx"
    document.write_bytes(b"uploaded document")
    filename = "原始报告.docx"
    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        token = media_service.register(
            conn, scope_id=scope_id, session_id="sess_x", kind="file", source="user_upload",
            local_path=str(document.resolve()), file_name=filename,
        )
    client = ui_server.app.test_client()
    assert client.get(f"/api/media/{token}/meta").get_json()["name"] == filename
    response = client.get(f"/api/media/{token}?download=1")
    assert response.status_code == 200
    disposition = Message()
    disposition["Content-Disposition"] = response.headers["Content-Disposition"]
    assert disposition.get_filename() == filename
    engine.dispose()


def test_rewrite_angle_wrapped_file_links(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    image = tmp_path / "图片 文件.png"
    image.write_bytes(b"image")
    report = tmp_path / "My Report (最终).md"
    report.write_text("report", encoding="utf-8")
    text = f"![图片](<file://{image}>) and [下载报告](<file://{report}>)"

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)

    assert "file://" not in out
    assert out.startswith("![图片](</api/media/")
    assert "and [下载报告](</api/media/" in out
    with engine.connect() as conn:
        rows = conn.execute(select(media_objects)).mappings().all()
    assert {row["local_path"] for row in rows} == {
        str(image.resolve()),
        str(report.resolve()),
    }
    assert sorted(row["kind"] for row in rows) == ["file", "image"]


def test_rewrite_legacy_bare_space_link_and_reject_malformed_authority(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    report = tmp_path / "My Report.md"
    report.write_text("report", encoding="utf-8")
    malformed = "[bad](<file://[bad/path>)"
    text = f'[report](file://{report} "download") and {malformed}'

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(
            conn,
            scope_id=scope_id,
            session_id="sess_x",
            text=text,
        )

    assert out.startswith("[report](/api/media/")
    assert out.endswith(f' "download") and {malformed}')
    with engine.connect() as conn:
        rows = conn.execute(select(media_objects)).mappings().all()
    assert [row["local_path"] for row in rows] == [str(report.resolve())]


def test_rewrite_legacy_bare_space_link_after_malformed_prefix(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    report = tmp_path / "Good Report.md"
    report.write_text("report", encoding="utf-8")
    prefix = "[bad](file:///tmp/My Report "
    text = f"{prefix}[report](file://{report})"

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(
            conn,
            scope_id=scope_id,
            session_id="sess_x",
            text=text,
        )

    assert out.startswith(f"{prefix}[report](/api/media/")
    with engine.connect() as conn:
        rows = conn.execute(select(media_objects)).mappings().all()
    assert [row["local_path"] for row in rows] == [str(report.resolve())]


def test_rewrite_does_not_materialize_file_links_inside_code(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    image = tmp_path / "code.png"
    image.write_bytes(b"image")
    text = f"Example `![code](<file://{image}>)` and:\n\n```md\n![fenced](<file://{image}>)\n```"

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)

    assert out == text
    with engine.connect() as conn:
        assert conn.execute(select(media_objects)).first() is None


def test_resolve_attachment_specs(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 hello")

    from core.workbench_media import resolve_attachment_specs

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        token = media_service.register(
            conn,
            scope_id=scope_id,
            session_id="sess_x",
            kind="file",
            source="user_upload",
            local_path=str(doc),
            file_name="doc.pdf",
        )

    with engine.connect() as conn:
        specs = resolve_attachment_specs(
            conn,
            session_id="sess_x",
            attachments=[{"token": token}, {"token": "bad"}, {"nope": 1}],
        )
        cross = resolve_attachment_specs(conn, session_id="other", attachments=[{"token": token}])

    assert len(specs) == 1
    assert specs[0]["path"] == str(doc)
    assert specs[0]["mimetype"] == "application/pdf"
    assert specs[0]["name"] == "doc.pdf"
    # A token from another session must not resolve (defense in depth).
    assert cross == []


def test_message_context_accepts_files():
    # internal_server builds MessageContext(files=...) for web turns; guard the
    # contract that the dataclass takes a files kwarg.
    from modules.im.base import FileAttachment, MessageContext

    ctx = MessageContext(
        user_id="u",
        channel_id="c",
        platform="avibe",
        files=[FileAttachment(name="a.png", mimetype="image/png", local_path="/tmp/a.png")],
    )
    assert ctx.files and ctx.files[0].local_path == "/tmp/a.png"


def test_rewrite_noop_without_file_links(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    text = "Plain reply with a [link](https://example.com) and no files."
    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)
    assert out == text
    with engine.connect() as conn:
        assert conn.execute(select(media_objects)).first() is None


def test_process_reply_keep_file_links():
    # The avibe result path persists with keep_file_links=True so the proxy
    # rewrite can still see the file:// links; the IM default strips them.
    from core.reply_enhancer import process_reply

    raw = "Here ![chart](file:///tmp/c.png) and [doc](file:///tmp/d.pdf)\n\n---\n[OK]"

    default = process_reply(raw)
    assert "file://" not in default.text
    assert "![chart]" not in default.text
    assert len(default.files) == 2

    kept = process_reply(raw, keep_file_links=True)
    assert "![chart](file:///tmp/c.png)" in kept.text
    assert "[doc](file:///tmp/d.pdf)" in kept.text
    assert "[OK]" not in kept.text  # trailing quick-reply block still stripped
    assert len(kept.files) == 2


def test_rewrite_allows_any_absolute_path(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    from core.workbench_media import rewrite_agent_media

    # Any absolute path the agent references is proxied — it's the user's own
    # machine and the agent already has full FS read, so the proxy grants nothing
    # new. A file outside any "project" dir is rewritten + resolved canonically.
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    text = f"![shot](file://{outside})"
    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)
    assert "/api/media/" in out
    assert "file://" not in out
    with engine.connect() as conn:
        rows = conn.execute(select(media_objects)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["local_path"] == str(outside.resolve())


def test_register_dedups_same_fingerprint(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"abc")

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        conn.execute(
            agent_sessions.insert().values(
                id="other",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="other-anchor",
                native_session_id="other-native",
                status="active",
                metadata_json="{}",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        t1 = media_service.register(
            conn, scope_id=scope_id, session_id="sess_x", kind="image",
            source="agent_reply", local_path=str(shot),
        )
        # Same file in the same authorization scope stays stable + cacheable.
        assert media_service.register(
            conn, scope_id=scope_id, session_id="sess_x", kind="image",
            source="agent_reply", local_path=str(shot),
        ) == t1
        # A different session receives a distinct token so authorization checks
        # use that referencing session instead of the first dedup registration.
        t2 = media_service.register(
            conn, scope_id=scope_id, session_id="other", kind="image",
            source="agent_reply", local_path=str(shot),
        )
        assert t2 != t1
        # Content change (new size + mtime) → fresh token, busting the cache.
        shot.write_bytes(b"abcdef-changed")
        t3 = media_service.register(
            conn, scope_id=scope_id, session_id="sess_x", kind="image",
            source="agent_reply", local_path=str(shot),
        )
        assert t3 != t1

    with engine.connect() as conn:
        rows = conn.execute(select(media_objects)).mappings().all()
        references = conn.execute(select(media_object_references)).mappings().all()
    assert len(rows) == 3  # two authorization scopes + changed content
    assert {(row["token"], row["session_id"]) for row in references} == {
        (t1, "sess_x"),
        (t2, "other"),
        (t3, "sess_x"),
    }


def test_register_reads_image_dimensions(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    img = tmp_path / "wide.png"
    img.write_bytes(_png_bytes(120, 48))
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 hello")

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        img_token = media_service.register(
            conn, scope_id=scope_id, session_id="sess_x", kind="image",
            source="user_upload", local_path=str(img), file_name="wide.png",
        )
        doc_token = media_service.register(
            conn, scope_id=scope_id, session_id="sess_x", kind="file",
            source="user_upload", local_path=str(doc), file_name="report.pdf",
        )

    with engine.connect() as conn:
        img_row = media_service.get_by_token(conn, img_token)
        doc_row = media_service.get_by_token(conn, doc_token)

    # The image's real header dimensions are captured…
    assert (img_row["width_px"], img_row["height_px"]) == (120, 48)
    # …while a non-image (and any file the probe can't read) stays NULL.
    assert doc_row["width_px"] is None and doc_row["height_px"] is None


def test_register_image_dimensions_unreadable_is_null(tmp_path, sqlite_schema_db_factory):
    # A file flagged as an image but not actually decodable must not break
    # registration — dimensions degrade to NULL (UI measures it in the browser).
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    bogus = tmp_path / "broken.png"
    bogus.write_bytes(b"not a real png")

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        token = media_service.register(
            conn, scope_id=scope_id, session_id="sess_x", kind="image",
            source="user_upload", local_path=str(bogus), file_name="broken.png",
        )

    with engine.connect() as conn:
        row = media_service.get_by_token(conn, token)
    assert row["width_px"] is None and row["height_px"] is None


def test_rewrite_appends_image_dimensions(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)

    img = tmp_path / "chart.png"
    img.write_bytes(_png_bytes(64, 32))
    doc = tmp_path / "notes.pdf"
    doc.write_bytes(b"%PDF-1.4")

    text = f"![chart](file://{img}) and [notes](file://{doc})"
    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)

    # Image proxy URL carries the pixel size so the browser reserves the box…
    assert "?w=64&h=32)" in out
    # …and a non-image link gets no dimension query.
    assert "/api/media/" in out.split(" and ")[1]
    assert "?w=" not in out.split(" and ")[1]


def test_rewrite_angle_file_link_unescapes_path_and_ignores_title(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    report = tmp_path / "report (final).md"
    report.write_text("report", encoding="utf-8")
    escaped_report = str(report).replace("(", "\\(").replace(")", "\\)")
    text = f'[report](<FILE://{escaped_report}> "download")'

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)

    assert out.startswith("[report](</api/media/")
    assert out.endswith('> "download")')


def test_rewrite_commonmark_owned_destination_preserves_source_syntax(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    report = tmp_path / "report & (final).md"
    report.write_text("report", encoding="utf-8")
    escaped_report = (
        str(report)
        .replace("&", "&amp;")
        .replace("(", r"\(")
        .replace(")", r"\)")
    )
    text = (
        f'[outer [report](<f&#105;le://{escaped_report}>\n "download")]'
        " and [draft](<file:relative.md>)"
    )

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        out = rewrite_agent_media(conn, scope_id=scope_id, session_id="sess_x", text=text)

    assert out.startswith("[outer [report](</api/media/")
    assert out.endswith('>\n "download")] and [draft](<file:relative.md>)')
    with engine.connect() as conn:
        rows = conn.execute(select(media_objects)).mappings().all()
    assert [row["local_path"] for row in rows] == [str(report.resolve())]


def test_rewrite_failure_preserves_original_destination_source(tmp_path, sqlite_schema_db_factory):
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    text = '[report](<f&#105;le:///tmp/a&amp;b.md> "download")'

    with engine.begin() as conn:
        scope_id = _seed_scope_and_session(conn)
        with patch(
            "core.workbench_media.register_agent_reply_media",
            side_effect=RuntimeError("registration failed"),
        ):
            out = rewrite_agent_media(
                conn,
                scope_id=scope_id,
                session_id="sess_x",
                text=text,
            )

    assert out == text
