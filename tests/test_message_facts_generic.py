"""Generic native message classification across the IM adapters."""

from types import SimpleNamespace

from modules.im.message_facts import (
    discord_message_kind,
    feishu_message_kind,
    is_original_human_discord_text,
    is_original_human_feishu_text,
    is_original_human_slack_text,
    is_original_human_telegram_text,
    is_original_human_wechat_text,
    slack_message_kind,
    telegram_message_kind,
    wechat_message_kind,
)


def test_native_human_text_classification_covers_all_im_adapters() -> None:
    discord = SimpleNamespace(author=SimpleNamespace(bot=False), edited_at=None, attachments=[], embeds=[],
                              flags=SimpleNamespace(forwarded=False), message_snapshots=(), is_system=lambda: False)
    assert is_original_human_discord_text(discord, None)
    discord.flags.forwarded = True
    assert not is_original_human_discord_text(discord, None)
    assert is_original_human_slack_text({"text": "hello"}, None)
    assert not is_original_human_slack_text({"text": "hello", "subtype": "message_changed"}, None)
    assert is_original_human_telegram_text({"from": {"is_bot": False}, "text": "hello"}, [])
    assert not is_original_human_telegram_text({"from": {"is_bot": False}, "forward_origin": {"type": "user"}}, [])
    feishu = {"sender": {"sender_type": "user"}, "message": {"message_type": "text"}}
    assert is_original_human_feishu_text(feishu, None, shared_text=None)
    feishu["message"]["message_type"] = "post"
    assert not is_original_human_feishu_text(feishu, None, shared_text=None)
    assert is_original_human_wechat_text({"item_list": [{"type": "TEXT"}]}, None)
    assert not is_original_human_wechat_text({"item_list": [{"type": 1, "ref_msg": {"title": "quoted"}}]}, None)


def test_native_message_kind_preserves_forwarded_and_edited_semantics() -> None:
    discord = SimpleNamespace(author=SimpleNamespace(bot=False), edited_at=None, attachments=[], embeds=[],
                              flags=SimpleNamespace(forwarded=True), message_snapshots=(), is_system=lambda: False)
    assert discord_message_kind(discord, None) == "forwarded"
    assert slack_message_kind({"text": "edited", "subtype": "message_changed"}, None) == "edited"
    assert telegram_message_kind({"from": {"is_bot": False}, "is_system": True}, []) == "system"
    assert feishu_message_kind({"sender": {"sender_type": "user"}, "message": {"message_type": "text", "forwarded": True}}, {}, None, shared_text="x") == "forwarded"
    assert wechat_message_kind({"item_list": [{"type": 1, "ref_msg": {"title": "quoted"}}]}, None) == "forwarded"


def test_slack_rich_text_composer_is_human_but_non_text_blocks_are_not() -> None:
    event = {"type": "message", "text": "hello", "channel_type": "im", "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hello"}]}]}]}
    assert is_original_human_slack_text(event, None)
    event["blocks"] = [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}]
    assert not is_original_human_slack_text(event, None)
    upload = {"type": "message", "text": "look", "subtype": "file_share", "channel_type": "im", "files": [{"id": "F1"}]}
    assert not is_original_human_slack_text(upload, None)
    assert is_original_human_slack_text({"type": "message", "text": "plain", "channel_type": "im"}, None)
