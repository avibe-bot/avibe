# A platform pass needs CommonMark's own semantics as well as its dialect's:
# a character reference is resolved where the reader's characters are decided,
# next to the escapes and the links this package already hands out.
from core.reply_enhancer import resolve_character_references

from .base_formatter import (
    BaseMarkdownFormatter,
    hold_links,
    hold_markdown_escapes,
    restore_held,
)
from .slack_formatter import SlackFormatter, encode_slack_delimiters
from .discord_formatter import DiscordFormatter
from .telegram_formatter import TelegramFormatter
from .feishu_formatter import FeishuFormatter
from .wechat_formatter import WeChatFormatter
from .avibe_formatter import AvibeFormatter

__all__ = [
    "BaseMarkdownFormatter",
    "hold_links",
    "hold_markdown_escapes",
    "resolve_character_references",
    "restore_held",
    "SlackFormatter",
    "encode_slack_delimiters",
    "DiscordFormatter",
    "TelegramFormatter",
    "FeishuFormatter",
    "WeChatFormatter",
    "AvibeFormatter",
]
