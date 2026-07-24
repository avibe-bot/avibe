"""Contracts for the shared, per-user Memory capture boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.controller import Controller
from core.memory import CaptureAccepted, CaptureDuplicate
from modules.im.base import FileAttachment, MessageContext
from modules.im.message_facts import (
    is_ordinary_discord_text,
    is_ordinary_feishu_text,
    is_ordinary_slack_text,
    is_ordinary_telegram_text,
    is_ordinary_wechat_text,
)


class _Store:
    def __init__(self, user) -> None:
        self.user = user

    def maybe_reload(self) -> None:
        return None

    def get_user(self, _user_id: str, *, platform: str):
        return self.user


class _Manager:
    def __init__(self, user) -> None:
        self.store = _Store(user)

    def get_store(self):
        return self.store


class _Runtime:
    def principal_for_user_key(self, user_key: str) -> str:
        suffix = "1" if user_key.endswith("user-1") else "2"
        return f"u-{suffix * 32}"


class _CaptureModule:
    def __init__(self) -> None:
        self.accepted = []
        self.seen: set[str] = set()

    async def capture(self, request):
        if request.source_message_id in self.seen:
            return CaptureDuplicate()
        self.seen.add(request.source_message_id)
        self.accepted.append(request)
        return CaptureAccepted()


def _controller(*, user=None):
    user = user or SimpleNamespace(enabled=True, is_admin=False)
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    controller.platform_settings_managers = {
        platform: _Manager(user)
        for platform in ("slack", "discord", "telegram", "feishu", "wechat", "lark")
    }
    controller.memory_runtime = _Runtime()
    controller.memory_module = _CaptureModule()
    return controller


def _context(platform: str, *, user_id: str = "user-1", ordinary=True, **payload) -> MessageContext:
    return MessageContext(
        user_id=user_id,
        channel_id="dm-1",
        platform=platform,
        message_id="native-1",
        platform_specific={"platform": platform, "is_dm": True, **payload},
        files=[],
        is_ordinary_text=ordinary,
    )


@pytest.mark.parametrize("platform", ["slack", "discord", "telegram", "feishu", "wechat"])
def test_capture_admits_every_enabled_bound_dm_user(platform: str) -> None:
    controller = _controller(user=SimpleNamespace(enabled=True, is_admin=False))

    assert controller.memory_capture_admitted(_context(platform)) is True
    assert controller.memory_capture_admitted(_context(platform, is_dm=False)) is False


@pytest.mark.parametrize(
    "context,text,enabled",
    [
        (_context("slack", is_dm=False), "normal", True),
        (_context("slack", ordinary=False), "normal", True),
        (_context("slack", user_id=""), "normal", True),
        (_context("avibe", user_id="workbench"), "normal", True),
        (_context("slack"), "normal", False),
    ],
)
def test_capture_skips_ineligible_human_turns(context, text, enabled) -> None:
    controller = _controller()
    controller.config.memory.enabled = enabled

    asyncio.run(controller.capture_user_memory(context, text, "stable-session"))

    assert controller.memory_module.accepted == []


def test_capture_stamps_user_principal_provenance_and_native_dedup_key() -> None:
    controller = _controller()
    context = _context("telegram")

    asyncio.run(controller.capture_user_memory(context, "/memory status", "stable-session"))
    asyncio.run(controller.capture_user_memory(context, "/memory status", "stable-session"))

    assert len(controller.memory_module.accepted) == 1
    request = controller.memory_module.accepted[0]
    assert request.source_message_id == "im:telegram:native-1"
    assert request.session_id == "stable-session"
    assert request.principal_id == "u-" + ("1" * 32)
    assert request.provenance == "user_input"
    assert request.text == "/memory status"


def test_workbench_capture_requires_resolved_identity_and_uses_row_id() -> None:
    controller = _controller()
    context = _context("avibe", user_id="local")

    asyncio.run(controller.capture_user_memory(context, "ordinary text", "stable-session"))

    request = controller.memory_module.accepted[0]
    assert request.source_message_id == "workbench:native-1"
    assert request.principal_id == "u-" + ("2" * 32)


def test_workbench_capture_converts_owned_attachment_without_text(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    attachment_path = tmp_path / "attachments" / "avibe" / "receipt.pdf"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(b"pdf")
    controller = _controller()
    context = _context("avibe", user_id="local")
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            local_path=str(attachment_path),
        )
    ]

    asyncio.run(controller.capture_user_memory(context, "", "stable-session"))

    request = controller.memory_module.accepted[0]
    assert request.text == ""
    assert request.attachments[0].kind == "pdf"
    assert request.attachments[0].name == "receipt.pdf"
    assert request.attachments[0].uri == attachment_path.as_uri()


def test_im_attachments_remain_out_of_scope() -> None:
    controller = _controller()
    context = _context("slack")
    context.files = [object()]

    asyncio.run(controller.capture_user_memory(context, "ordinary text", "stable-session"))

    assert controller.memory_module.accepted == []


def test_im_adapters_normalize_native_ordinary_text_facts() -> None:
    discord_message = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        edited_at=None,
        attachments=[],
        embeds=[],
        flags=SimpleNamespace(forwarded=False),
        message_snapshots=(),
        is_system=lambda: False,
    )
    assert is_ordinary_discord_text(discord_message, None) is True
    discord_message.flags.forwarded = True
    assert is_ordinary_discord_text(discord_message, None) is False

    assert is_ordinary_slack_text({"text": "hello"}, None) is True
    assert is_ordinary_slack_text({"text": "hello", "subtype": "message_changed"}, None) is False

    assert is_ordinary_telegram_text({"from": {"is_bot": False}, "text": "hello"}, []) is True
    assert is_ordinary_telegram_text({"from": {"is_bot": False}, "forward_origin": {"type": "user"}}, []) is False

    feishu_event = {"sender": {"sender_type": "user"}, "message": {"message_type": "text"}}
    assert is_ordinary_feishu_text(feishu_event, None, shared_text=None) is True
    feishu_event["message"]["message_type"] = "post"
    assert is_ordinary_feishu_text(feishu_event, None, shared_text=None) is False

    assert is_ordinary_wechat_text({"item_list": [{"type": "TEXT"}]}, None) is True
    assert is_ordinary_wechat_text({"item_list": [{"type": 1}, {"type": 2}]}, None) is False
    assert is_ordinary_wechat_text({"item_list": [{"type": 1, "ref_msg": {"title": "quoted"}}]}, None) is False


def test_slack_manifest_has_no_native_memory_command() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "vibe" / "templates" / "slack_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    commands = manifest["features"].get("slash_commands", [])
    assert all(command.get("command") != "/memory" for command in commands)
