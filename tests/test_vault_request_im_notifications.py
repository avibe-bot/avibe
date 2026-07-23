from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.vault_request_notifications import notify_vault_request_created
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, metadata as db_metadata, scopes


class FakeController:
    def __init__(self, *, public_url: str = "https://desk.avibe.bot/u/team") -> None:
        self.config = SimpleNamespace(
            language="en",
            remote_access=SimpleNamespace(
                vibe_cloud=SimpleNamespace(enabled=bool(public_url), public_url=public_url),
            ),
        )
        self.platform_settings_managers: dict[str, Any] = {}
        self.emitted: list[dict[str, Any]] = []

    async def emit_agent_message(
        self,
        context,
        *,
        message_type: str,
        text: str,
        parse_mode: str = "markdown",
    ) -> str:
        self.emitted.append(
            {
                "context": context,
                "message_type": message_type,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        return "msg-1"


def _seed_session(
    db_path: Path,
    *,
    session_id: str = "ses_im",
    platform: str = "slack",
    scope_type: str = "channel",
    native_id: str = "C123",
    session_anchor: str | None = None,
    visibility: str = "foreground",
    metadata_json: str = "{}",
) -> None:
    engine = create_sqlite_engine(db_path)
    db_metadata.create_all(engine)
    now = datetime.now(timezone.utc).isoformat()
    scope_id = f"{platform}_{scope_type}_{native_id}"
    with engine.begin() as conn:
        conn.execute(
            scopes.insert().values(
                id=scope_id,
                platform=platform,
                scope_type=scope_type,
                native_id=native_id,
                parent_scope_id=None,
                display_name=None,
                native_type=None,
                is_private=1 if scope_type == "user" else 0,
                supports_threads=1,
                metadata_json="{}",
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_id=None,
                agent_name="codex",
                agent_backend="codex",
                agent_variant="default",
                model=None,
                reasoning_effort=None,
                session_anchor=session_anchor or f"{platform}_{native_id}",
                workdir="/tmp/work",
                native_session_id="native-1",
                title=None,
                status="active",
                visibility=visibility,
                agent_status="idle",
                metadata_json=metadata_json,
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    engine.dispose()


def test_notifies_im_session_with_platform_link_format(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_session(db_path, session_anchor="slack_1719990000.1")
    controller = FakeController()

    result = asyncio.run(
        notify_vault_request_created(
            controller,
            {
                "id": "vrq_123",
                "request_type": "access",
                "secret_name": "API_KEY",
                "status": "pending",
                "requester": {"session_id": "ses_im"},
                "card": {"request_type": "access", "secret_names": ["API_KEY"]},
            },
            db_path=db_path,
        )
    )

    assert result["sent"] is True
    assert len(controller.emitted) == 1
    emitted = controller.emitted[0]
    context = emitted["context"]
    assert emitted["message_type"] == "notify"
    assert emitted["parse_mode"] == "markdown"
    assert context.platform == "slack"
    assert context.channel_id == "C123"
    assert context.thread_id == "1719990000.1"
    assert context.platform_specific["agent_session_id"] == "ses_im"
    assert "API_KEY" in emitted["text"]
    assert "<https://desk.avibe.bot/u/team/vaults?request_id=vrq_123|Review on the web>" in emitted["text"]


def test_notifies_wechat_with_plain_url_format(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_session(db_path, platform="wechat", native_id="wx-chat")
    controller = FakeController(public_url="https://desk.avibe.bot")

    asyncio.run(
        notify_vault_request_created(
            controller,
            {
                "id": "vrq_wx",
                "request_type": "sign",
                "secret_name": "WALLET_KEY",
                "status": "pending",
                "delivery": {"session_id": "ses_im"},
                "card": {"request_type": "sign", "secret_names": ["WALLET_KEY"]},
            },
            db_path=db_path,
        )
    )

    assert len(controller.emitted) == 1
    text = controller.emitted[0]["text"]
    assert "Review on the web (https://desk.avibe.bot/vaults?request_id=vrq_wx)" in text


def test_provision_request_can_notify_from_secure_input_card(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_session(db_path)
    controller = FakeController()

    result = asyncio.run(
        notify_vault_request_created(
            controller,
            {
                "id": "vrq_provision",
                "secret_name": "NEW_TOKEN",
                "status": "pending",
                "card": {
                    "card_type": "secure_input",
                    "request_id": "vrq_provision",
                    "secret_name": "NEW_TOKEN",
                    "session_id": "ses_im",
                    "value": None,
                },
            },
            db_path=db_path,
        )
    )

    assert result["sent"] is True
    assert "A secret to fill in" in controller.emitted[0]["text"]
    assert "NEW_TOKEN" in controller.emitted[0]["text"]


def test_workbench_session_is_not_notified(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_session(
        db_path,
        session_id="ses_web",
        platform="avibe",
        scope_type="project",
        native_id="proj_1",
        session_anchor="avibe_ses_web",
    )
    controller = FakeController()

    result = asyncio.run(
        notify_vault_request_created(
            controller,
            {
                "id": "vrq_web",
                "request_type": "access",
                "secret_name": "API_KEY",
                "status": "pending",
                "requester": {"session_id": "ses_web"},
            },
            db_path=db_path,
        )
    )

    assert result["sent"] is False
    assert result["reason"] == "workbench_session"
    assert controller.emitted == []


def test_background_session_is_not_notified(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_session(db_path, visibility="background")
    controller = FakeController()

    result = asyncio.run(
        notify_vault_request_created(
            controller,
            {
                "id": "vrq_suppressed",
                "request_type": "access",
                "secret_name": "API_KEY",
                "status": "pending",
                "requester": {"session_id": "ses_im"},
            },
            db_path=db_path,
        )
    )

    assert result["sent"] is False
    assert result["reason"] == "delivery_suppressed"
    assert controller.emitted == []
