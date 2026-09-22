"""Organization Chat rows name their sender; personal ones stay as they were.

The label has to be identical however the row arrives -- the ``message.new``
frame published on delivery, the tail the page hydrates with, the older page it
scrolls back into, the window it jumps to around a search hit -- or a bubble
would change sender between live and reload.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_settings import SettingsStore
from storage import messages_service, remote_access_authorization_service, sender_identity
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions
from storage.settings_service import upsert_scope
from tests.ui_server_test_helpers import _save_config

INSTANCE_ID = "inst_123"
SESSION_ID = "sess_org_identity"
AMY = "remote:sub-amy"
STRANGER = "remote:sub-stranger"


def _state(tmp_path, monkeypatch, *, instance_kind: str):
    """A configured instance with one session and one known Cloud member."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path, paired=True, instance_kind=instance_kind)
    ensure_sqlite_state()
    SettingsStore.reset_instance()

    remote_access_authorization_service.upsert_scoped(
        reference=None,
        instance_id=INSTANCE_ID,
        subject="sub-amy",
        email="amy.chen@acme.example",
        scope_kind="instance",
        scope_ref=INSTANCE_ID,
        authorization_state="current",
        claims={"email": "amy.chen@acme.example"},
        last_checked_at=1,
        updated_at=1,
    )

    engine = create_sqlite_engine(tmp_path / "state" / "vibe.sqlite")
    with engine.begin() as conn:
        now = messages_service._utc_now_iso()
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_org", now=now
        )
        conn.execute(
            agent_sessions.insert().values(
                id=SESSION_ID,
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_" + SESSION_ID,
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    return engine, scope_id


def _seed_transcript(engine, scope_id: str) -> dict[str, str]:
    """Four rows: two Cloud humans, the loopback owner, and the agent."""

    ids: dict[str, str] = {}
    with engine.begin() as conn:
        for key, author, author_id in (
            ("amy", "user", AMY),
            ("stranger", "user", STRANGER),
            ("owner", "user", "local"),
            ("agent", "agent", None),
        ):
            ids[key] = messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=SESSION_ID,
                platform="avibe",
                author=author,
                text=f"row from {key}",
                author_id=author_id,
            )["id"]
    return ids


def _window(conn, **kwargs) -> dict[str, dict]:
    result = messages_service.list_session_messages(
        conn, session_id=SESSION_ID, limit=50, **kwargs
    )
    return {row["id"]: row for row in result["messages"]}


def test_organization_transcript_labels_every_window_and_the_live_row(tmp_path, monkeypatch):
    engine, scope_id = _state(tmp_path, monkeypatch, instance_kind="organization")
    ids = _seed_transcript(engine, scope_id)

    with engine.connect() as conn:
        tail = _window(conn, tail=True)
        older = _window(conn, before_id=ids["agent"])
        around = _window(conn, around_id=ids["amy"])
        live = messages_service.get_message(conn, ids["amy"])

    for window in (tail, older, around):
        assert window[ids["amy"]]["sender_label"] == "amy.chen"
    # The live frame and every reload window agree, so a bubble keeps its
    # sender when the page refetches.
    assert live["sender_label"] == "amy.chen"

    # A subject with no stored authorization stays unnamed rather than failing
    # the page, and the loopback owner and the agent are untouched.
    assert "sender_label" not in tail[ids["stranger"]]
    assert "sender_label" not in tail[ids["owner"]]
    assert "sender_label" not in tail[ids["agent"]]


def test_organization_payload_carries_the_label_not_the_address(tmp_path, monkeypatch):
    engine, scope_id = _state(tmp_path, monkeypatch, instance_kind="organization")
    _seed_transcript(engine, scope_id)

    with engine.connect() as conn:
        result = messages_service.list_session_messages(
            conn, session_id=SESSION_ID, tail=True, limit=50
        )

    assert "acme.example" not in repr(result)
    assert any(row.get("sender_label") == "amy.chen" for row in result["messages"])


def test_personal_instance_transcript_is_unchanged(tmp_path, monkeypatch):
    engine, scope_id = _state(tmp_path, monkeypatch, instance_kind="personal")
    ids = _seed_transcript(engine, scope_id)

    with engine.connect() as conn:
        tail = _window(conn, tail=True)
        live = messages_service.get_message(conn, ids["amy"])

    assert all("sender_label" not in row for row in tail.values())
    assert "sender_label" not in live


def test_organization_transcript_survives_an_unreadable_identity_source(tmp_path, monkeypatch):
    engine, scope_id = _state(tmp_path, monkeypatch, instance_kind="organization")
    ids = _seed_transcript(engine, scope_id)
    with engine.begin() as conn:
        conn.execute(text("drop table remote_access_authorizations"))

    with engine.connect() as conn:
        tail = _window(conn, tail=True)

    assert set(tail) == set(ids.values())
    assert "sender_label" not in tail[ids["amy"]]


def test_show_page_audiences_do_not_receive_the_sender_label():
    from core import show_session_events

    projected = show_session_events._public_event_message(
        {"id": "msg_1", "text": "hi", "sender_label": "amy.chen", "metadata": {}}
    )

    # A Show Page audience -- a shared link included -- sits outside the
    # Instance, so the anchored row keeps its pre-#2126 shape.
    assert "sender_label" not in projected
    assert projected["text"] == "hi"
    assert show_session_events._public_event_message(None) is None


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("amy.chen@acme.example", "amy.chen"),
        ("  amy@acme.example  ", "amy"),
        ("@acme.example", "@acme.example"),
        ("", None),
        (None, None),
    ],
)
def test_sender_label_is_the_local_part(email, expected):
    assert sender_identity.sender_label_from_email(email) == expected
