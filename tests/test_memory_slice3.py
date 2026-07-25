"""Contracts for the shared, per-user Memory capture boundary."""

from __future__ import annotations

import asyncio
import gc
import json
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.controller import Controller
from core.handlers.message_handler import MessageHandler
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
    """Stand in for MemoryRuntime, which owns the capture module."""

    def __init__(self, module) -> None:
        self.module = module

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
    controller.memory_module = _CaptureModule()
    controller.memory_runtime = _Runtime(controller.memory_module)
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


def test_message_handler_retains_memory_capture_task_until_completion() -> None:
    handler = MessageHandler.__new__(MessageHandler)
    handler._memory_capture_tasks = set()

    async def run() -> None:
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        reference = weakref.ref(task)
        handler._track_memory_capture_task(task)
        del task
        gc.collect()

        assert reference() is not None
        assert len(handler._memory_capture_tasks) == 1

        release.set()
        await reference()
        await asyncio.sleep(0)
        assert handler._memory_capture_tasks == set()

    asyncio.run(run())


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
    other_user = _context("telegram", user_id="user-2")

    asyncio.run(controller.capture_user_memory(context, "/memory status", "stable-session"))
    asyncio.run(controller.capture_user_memory(context, "/memory status", "stable-session"))
    asyncio.run(controller.capture_user_memory(other_user, "/memory status", "stable-session"))

    assert len(controller.memory_module.accepted) == 2
    request = controller.memory_module.accepted[0]
    assert request.source_message_id == f"im:telegram:u-{'1' * 32}:native-1"
    assert request.session_id == "stable-session"
    assert request.principal_id == "u-" + ("1" * 32)
    assert request.provenance == "user_input"
    assert request.text == "/memory status"
    assert controller.memory_module.accepted[1].source_message_id == f"im:telegram:u-{'2' * 32}:native-1"


def test_workbench_capture_requires_resolved_identity_and_uses_row_id() -> None:
    controller = _controller()
    context = _context("avibe", user_id="local")

    asyncio.run(controller.capture_user_memory(context, "ordinary text", "stable-session"))

    request = controller.memory_module.accepted[0]
    assert request.source_message_id == f"workbench:u-{'2' * 32}:native-1"
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


def _slack_dm_event(**overrides) -> dict:
    """Return a real-shaped Slack ``message`` event for a DM typed in a client.

    Modern Slack clients always attach the composer's ``rich_text`` block, so a
    payload without ``blocks`` does not represent what production delivers.
    """

    event = {
        "client_msg_id": "3d0a24a2-1c1a-4b6f-9f43-8f9d0d9a1111",
        "type": "message",
        "text": "ship the memory fix today",
        "user": "U04ABCDEF",
        "ts": "1753420800.123456",
        "team": "T04ABCDEF",
        "channel": "D04ABCDEF",
        "channel_type": "im",
        "event_ts": "1753420800.123456",
        "blocks": [
            {
                "type": "rich_text",
                "block_id": "Xq2",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "ship the memory fix today"}],
                    }
                ],
            }
        ],
    }
    event.update(overrides)
    return event


def test_slack_composer_rich_text_dm_is_ordinary_human_text() -> None:
    assert is_ordinary_slack_text(_slack_dm_event(), None) is True

    # Mentions, links, emoji, styled runs, lists, quotes, and code blocks are all
    # plain composer output for a human-typed DM.
    decorated = _slack_dm_event(
        text="<@U04TEAMMATE> see <https://example.com|docs> :tada:",
        blocks=[
            {
                "type": "rich_text",
                "block_id": "d1F",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U04TEAMMATE"},
                            {"type": "text", "text": " see "},
                            {"type": "link", "url": "https://example.com", "text": "docs"},
                            {"type": "text", "text": " now", "style": {"bold": True}},
                            {"type": "emoji", "name": "tada", "unicode": "1f389"},
                        ],
                    },
                    {
                        "type": "rich_text_list",
                        "style": "bullet",
                        "indent": 0,
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "first"}],
                            }
                        ],
                    },
                    {
                        "type": "rich_text_quote",
                        "elements": [{"type": "text", "text": "quoted line"}],
                    },
                    {
                        "type": "rich_text_preformatted",
                        "elements": [{"type": "text", "text": "uv run pytest"}],
                    },
                ],
            }
        ],
    )
    assert is_ordinary_slack_text(decorated, None) is True


def test_slack_non_text_block_payloads_are_not_ordinary() -> None:
    # Image upload in a DM: Slack sends ``file_share`` with a files array.
    upload = _slack_dm_event(
        subtype="file_share",
        text="look at this",
        upload=False,
        display_as_bot=False,
        files=[
            {
                "id": "F04FILEID",
                "name": "screenshot.png",
                "mimetype": "image/png",
                "filetype": "png",
                "url_private_download": "https://files.slack.com/files-pri/T04-F04/screenshot.png",
            }
        ],
    )
    assert is_ordinary_slack_text(upload, None) is False

    # Forwarded / shared message: composer rich text PLUS a share attachment.
    forwarded = _slack_dm_event(
        text="fyi",
        attachments=[
            {
                "id": 1,
                "is_share": True,
                "author_name": "Teammate",
                "channel_id": "C04SOURCE",
                "ts": "1753410000.000100",
                "text": "the original message",
            }
        ],
    )
    assert is_ordinary_slack_text(forwarded, None) is False

    # App-authored layout blocks are not composer output, even without ``bot_id``.
    app_blocks = _slack_dm_event(
        text="Deployment finished",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "Deployment finished"}},
            {"type": "image", "image_url": "https://example.com/chart.png", "alt_text": "chart"},
        ],
    )
    assert is_ordinary_slack_text(app_blocks, None) is False

    # An unrecognized node inside rich text fails closed.
    unknown_element = _slack_dm_event(
        blocks=[
            {
                "type": "rich_text",
                "block_id": "u1",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "see "},
                            {"type": "image", "image_url": "https://example.com/inline.png"},
                        ],
                    }
                ],
            }
        ],
    )
    assert is_ordinary_slack_text(unknown_element, None) is False

    # Edits and bot/self events stay excluded regardless of block content.
    assert is_ordinary_slack_text(_slack_dm_event(subtype="message_changed"), None) is False
    assert (
        is_ordinary_slack_text(
            _slack_dm_event(edited={"user": "U04ABCDEF", "ts": "1753420900.000000"}),
            None,
        )
        is False
    )
    assert is_ordinary_slack_text(_slack_dm_event(bot_id="B04BOTID"), None) is False
    assert (
        is_ordinary_slack_text(
            _slack_dm_event(),
            [
                FileAttachment(
                    name="notes.txt",
                    mimetype="text/plain",
                    url="https://files.slack.com/notes.txt",
                )
            ],
        )
        is False
    )


def test_slack_manifest_has_no_native_memory_command() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "vibe" / "templates" / "slack_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    commands = manifest["features"].get("slash_commands", [])
    assert all(command.get("command") != "/memory" for command in commands)
