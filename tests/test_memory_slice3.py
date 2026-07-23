"""Contracts for Slice 3's shared Memory entry seams."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.controller import Controller
from core.memory import CaptureAccepted, CaptureDuplicate
from core.memory.commands import MAX_INERT_MEMORY_REPLY_BYTES, bounded_inert_text, parse_memory_command
from modules.im.base import MessageContext


class _Store:
    def __init__(self, user) -> None:
        self.user = user
        self.reloads = 0

    def maybe_reload(self) -> None:
        self.reloads += 1

    def get_user(self, _user_id: str, *, platform: str):
        return self.user


class _Manager:
    def __init__(self, user) -> None:
        self.store = _Store(user)

    def get_store(self):
        return self.store


class _InertClient:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def send_inert_message(self, _context, text: str) -> str:
        self.replies.append(text)
        return "reply-1"


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def status_payload(self):
        self.calls.append(("status", None))
        return {"state": "ready", "pending": 1, "processing": 0, "missed": 0}

    async def profile_payload(self):
        self.calls.append(("profile", None))
        return {"status": "ok", "items": [{"kind": "profile", "text": "@" + ("x" * 5000)}]}

    async def search_payload(self, query: str, limit: int):
        self.calls.append(("search", (query, limit)))
        return {"status": "ok", "items": [{"kind": "fact", "text": query}]}


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


_DEFAULT_USER = object()


def _controller(*, user=_DEFAULT_USER):
    if user is _DEFAULT_USER:
        user = SimpleNamespace(enabled=True, is_admin=True)
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    controller.platform_settings_managers = {
        platform: _Manager(user)
        for platform in ("slack", "discord", "telegram", "feishu", "wechat", "lark")
    }
    controller.memory_runtime = _Runtime()
    controller.memory_module = _CaptureModule()
    controller.client = _InertClient()
    controller.get_im_client_for_context = lambda _context: controller.client
    controller._t = lambda key, **kwargs: key.format(**kwargs)
    return controller


def _context(platform: str, **payload) -> MessageContext:
    return MessageContext(
        user_id="user-1",
        channel_id="dm-1",
        platform=platform,
        message_id="native-1",
        platform_specific={"platform": platform, "is_dm": True, **payload},
        files=[],
    )


@pytest.mark.parametrize("platform", ["slack", "discord", "telegram", "feishu", "wechat"])
def test_private_memory_contract_requires_dm_admin_and_bounds_reply(platform: str) -> None:
    controller = _controller()
    context = _context(platform)

    assert controller.memory_im_admitted(context) is True
    asyncio.run(controller.handle_memory_command(context, "profile"))

    assert controller.memory_runtime.calls == [("profile", None)]
    assert len(controller.client.replies) == 1
    assert len(controller.client.replies[0].encode("utf-8")) <= MAX_INERT_MEMORY_REPLY_BYTES
    assert "@" not in controller.client.replies[0]
    # The explicit assertion above plus the handler's fresh fail-closed check
    # each reload the bound-user record.
    assert controller.platform_settings_managers[platform].store.reloads == 2

    assert controller.memory_im_admitted(_context(platform, is_dm=False)) is False

    for user in (
        None,
        SimpleNamespace(enabled=False, is_admin=True),
        SimpleNamespace(enabled=True, is_admin=False),
    ):
        rejected_controller = _controller(user=user)
        asyncio.run(rejected_controller.handle_memory_command(context, "status"))
        assert rejected_controller.memory_runtime.calls == []
        assert rejected_controller.client.replies == ["memory.command.unavailable"]


@pytest.mark.parametrize(
    "payload,files,text",
    [
        ({"is_dm": False}, [], "normal"),
        ({"is_forwarded": True}, [], "normal"),
        ({"edited": True}, [], "normal"),
        ({"is_rich": True}, [], "normal"),
        ({}, [object()], "normal"),
        ({"scheduled": True}, [], "normal"),
        ({}, [], "/memory status"),
    ],
)
def test_private_memory_capture_rejects_ineligible_input(payload, files, text) -> None:
    controller = _controller()
    context = _context("slack", **payload)
    context.files = files

    asyncio.run(controller.capture_memory_from_im(context, text, "stable-session"))

    assert controller.memory_module.accepted == []


def test_private_memory_capture_uses_platform_native_dedup_key_once() -> None:
    controller = _controller()
    context = _context("telegram")

    asyncio.run(controller.capture_memory_from_im(context, "ordinary text", "stable-session"))
    asyncio.run(controller.capture_memory_from_im(context, "ordinary text", "stable-session"))

    assert len(controller.memory_module.accepted) == 1
    request = controller.memory_module.accepted[0]
    assert request.source_message_id == "im:telegram:native-1"
    assert request.session_id == "stable-session"


def test_private_memory_capture_keeps_equal_native_ids_distinct_per_platform() -> None:
    controller = _controller()
    slack_context = _context("slack")
    telegram_context = _context("telegram")

    asyncio.run(controller.capture_memory_from_im(slack_context, "ordinary text", "stable-session"))
    asyncio.run(controller.capture_memory_from_im(telegram_context, "ordinary text", "stable-session"))

    assert [request.source_message_id for request in controller.memory_module.accepted] == [
        "im:slack:native-1",
        "im:telegram:native-1",
    ]


def test_memory_command_grammar_is_closed_and_inert_text_is_byte_bounded() -> None:
    assert parse_memory_command("/memory") is not None
    assert parse_memory_command("/memory\r\nstatus").action == "status"
    assert parse_memory_command("/memory search useful context").query == "useful context"
    assert parse_memory_command("/memory clear").action == "invalid"
    assert parse_memory_command("/memory search").action == "invalid"
    assert parse_memory_command("/memory capture text").action == "invalid"
    assert parse_memory_command("/memory export").action == "invalid"
    assert parse_memory_command("/memory search " + ("x" * (8 * 1024 + 1))).action == "invalid"
    assert parse_memory_command("/memoryx status") is None
    inert = bounded_inert_text("\x1b[31m@" + ("x" * 5000))
    assert len(inert.encode("utf-8")) <= MAX_INERT_MEMORY_REPLY_BYTES
    assert "\x1b" not in inert
    assert "@" not in inert


def test_private_memory_command_claims_a_stable_native_event_before_read() -> None:
    controller = _controller()
    claims: list[tuple[str, str, str]] = []

    class _Sessions:
        def try_record_processed_message(self, channel_id, thread_id, message_id):
            claims.append((channel_id, thread_id, message_id))
            return False

    controller.sessions = _Sessions()
    asyncio.run(controller.handle_memory_command(_context("slack"), "status"))

    assert claims == [("memory-command:slack:dm-1", "native-1", "native-1")]
    assert controller.memory_runtime.calls == []
    assert controller.client.replies == []


def test_slack_manifest_declares_native_memory_command() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "vibe" / "templates" / "slack_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    commands = manifest["features"]["slash_commands"]
    assert commands == [
        {
            "command": "/memory",
            "description": "Read local Memory in an eligible administrator DM",
            "usage_hint": "status | profile | search <query>",
            "should_escape": False,
        }
    ]
