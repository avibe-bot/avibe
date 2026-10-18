"""What a CommonMark backslash escape becomes on each IM platform.

One Markdown text is delivered everywhere, and an escape in it means exactly
one thing: show this character, do not read it as syntax. No IM dialect knows
that. Telegram and Slack read the escaped character as markup of their own -
Slack's converter turns an escaped ``*`` into ``_``, so the reader is shown a
different character than the writer wrote - and WeChat, whose renderer is a
sequence of regex passes, lets an escaped ``]`` close a link label early and
leaks the raw Markdown around it.

So the escape is resolved once, in the shared layer, before any platform pass
runs, and the character it protected is held behind a placeholder until that
pass is done. Every case below is the same question on three dialects: does the
reader see the character, and nothing else?
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im.formatters.telegram_formatter import TelegramFormatter
from modules.im.formatters.wechat_formatter import WeChatFormatter
from modules.im.slack import SlackBot, SlackMarkdownConverter
from modules.im.wechat import WeChatBot

LINK = "https://s.example/p"


def telegram(text: str) -> str:
    return TelegramFormatter().render(text)


def slack(text: str) -> str:
    # The conversion is the whole unit under test; the rest of the client is
    # network plumbing this never reaches.
    bot = SlackBot.__new__(SlackBot)
    bot.markdown_converter = SlackMarkdownConverter()
    return bot._convert_markdown_to_slack_mrkdwn(text)


def wechat(text: str) -> str:
    bot = WeChatBot.__new__(WeChatBot)
    bot.formatter = WeChatFormatter()
    return bot.format_markdown(text)


DIALECTS = pytest.mark.parametrize(
    "render", [telegram, slack, wechat], ids=["telegram", "slack", "wechat"]
)


class TestEscapedProse:
    """An escape the agent wrote in ordinary prose, which predates citations."""

    @DIALECTS
    @pytest.mark.parametrize(
        "raw, shown",
        [
            (r"plain \*literal\* text", "plain *literal* text"),
            (r"a \_b\_ c", "a _b_ c"),
            (r"tick \` tick", "tick ` tick"),
            (r"a \~b\~ c", "a ~b~ c"),
            (r"a \\ b", "a \\ b"),
        ],
        ids=["asterisk", "underscore", "backtick", "tilde", "backslash"],
    )
    def test_the_reader_sees_the_character_and_no_backslash(self, render, raw, shown):
        assert render(raw) == shown

    @DIALECTS
    def test_an_escape_inside_a_code_span_is_still_code(self, render):
        """A backslash is not an escape inside code, so it stays a backslash."""
        assert "\\_" in render(r"`a \_ b`")


class TestEscapedLinkLabel:
    """A label the citation rewrite escaped, which is why the escapes are there.

    The rewrite escapes the characters that would otherwise let a label swallow
    the link around it. That is a CommonMark statement, so every platform that
    does not speak CommonMark has to be told the same thing in its own terms -
    and the link has to survive the telling.
    """

    @pytest.mark.parametrize(
        "raw, label",
        [
            (r"[a\]b](%s) tail" % LINK, "a]b"),
            (r"[ex\`ample.com](%s) tail" % LINK, "ex`ample.com"),
            (r"[a\*b](%s) tail" % LINK, "a*b"),
            (r"[a\[b](%s) tail" % LINK, "a[b"),
        ],
        ids=["bracket", "backtick", "asterisk", "open-bracket"],
    )
    def test_telegram_links_the_label_verbatim(self, raw, label):
        assert telegram(raw) == f'<a href="{LINK}">{label}</a> tail'

    @pytest.mark.parametrize(
        "raw, label",
        [
            (r"[a\]b](%s) tail" % LINK, "a]b"),
            (r"[ex\`ample.com](%s) tail" % LINK, "ex`ample.com"),
            (r"[a\*b](%s) tail" % LINK, "a*b"),
            (r"[a\[b](%s) tail" % LINK, "a[b"),
        ],
        ids=["bracket", "backtick", "asterisk", "open-bracket"],
    )
    def test_slack_links_the_label_verbatim(self, raw, label):
        assert slack(raw) == f"<{LINK}|{label}> tail"

    @pytest.mark.parametrize(
        "raw, label",
        [
            (r"[a\]b](%s) tail" % LINK, "a]b"),
            (r"[ex\`ample.com](%s) tail" % LINK, "ex`ample.com"),
            (r"[a\*b](%s) tail" % LINK, "a*b"),
            (r"[a\[b](%s) tail" % LINK, "a[b"),
        ],
        ids=["bracket", "backtick", "asterisk", "open-bracket"],
    )
    def test_wechat_shows_the_label_and_the_address_it_cannot_link(self, raw, label):
        """WeChat has no links, so attribution survives as label plus address."""
        assert wechat(raw) == f"{label} ({LINK}) tail"

    @DIALECTS
    def test_an_escaped_backtick_in_a_label_does_not_take_the_code_after_it(self, render):
        """The defect the escape exists to prevent, seen on each platform.

        Unescaped, the backtick opens a code span that runs past ``](url)`` to
        the next one and the link is never parsed at all.
        """
        rendered = render(rf"[ex\`ample.com]({LINK}) and `code` prose.")

        assert "ex`ample.com" in rendered
        assert LINK in rendered
        assert "](" not in rendered


class TestLiteralText:
    """Text that must reach the reader exactly as written (#2820): a search query, a path, a command.

    The formatter writes it once, as CommonMark escapes (``escape_special_chars``),
    and each transport resolves those into its own literal. ``*domain*`` is the
    reported case: a query that ran as ``*domain*`` must not be shown as an
    emphasised ``domain``.
    """

    QUERY = "*domain* _x_ `c` [l](u) <b> a&lt;b |t| ~s~ \\"

    def test_telegram_shows_every_character(self):
        literal = TelegramFormatter().escape_special_chars(self.QUERY)

        assert telegram(literal) == "*domain* _x_ `c` [l](u) &lt;b&gt; a&amp;lt;b |t| ~s~ \\"

    def test_wechat_shows_every_character(self):
        literal = WeChatFormatter().escape_special_chars(self.QUERY)

        assert wechat(literal) == self.QUERY

    def test_feishu_writes_each_character_as_the_entity_it_shows(self):
        """Measured by posting the card and reading back Feishu's own parse of it: every character is plain text."""
        import json

        from config.v2_config import LarkConfig
        from modules.im.feishu import FeishuBot
        from modules.im.formatters.feishu_formatter import FeishuFormatter

        bot = FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret"))
        card = json.loads(bot._build_card_json(FeishuFormatter().escape_special_chars(self.QUERY)))

        assert card["body"]["elements"][0]["content"] == (
            "&#42;domain&#42; &#95;x&#95; &#96;c&#96; &#91;l&#93;(u) &#60;b&#62; a&#38;lt;b "
            "&#124;t&#124; &#126;s&#126; &#92;"
        )

    def test_slack_markdown_blocks_and_discord_read_the_escapes_themselves(self):
        """Slack's ``markdown`` block (a status bubble) and Discord render CommonMark escapes as the character,
        measured on Slack by reading the posted block back as rich text. So the text they are handed is the escape."""
        from modules.im.formatters.discord_formatter import DiscordFormatter
        from modules.im.formatters.slack_formatter import SlackFormatter

        expected = r"\*domain\* \_x\_ \`c\` \[l\](u) \<b\> a\&lt;b \|t\| \~s\~ \\"
        assert DiscordFormatter().escape_special_chars(self.QUERY) == expected
        bot = SlackBot.__new__(SlackBot)
        blocks, _ = bot._build_status_blocks(SlackFormatter().escape_special_chars(self.QUERY), "working")
        assert blocks[0] == {"type": "markdown", "text": expected}

    def test_slack_mrkdwn_spells_the_delimiters_it_reads_as_markup(self):
        """mrkdwn has no escape for paired ``*``, ``_``, ``~`` or a backtick (measured: not even a zero-width space
        beside one stops it), but a bare ``&``, ``<`` or ``>`` is markup it can be told is not: ``<!channel>``."""
        assert slack(r"a \<!channel\> \& \&lt;") == "a &lt;!channel&gt; &amp; &amp;lt;"

    def test_a_tool_label_and_a_code_span_show_what_the_tool_was_given(self):
        from modules.im.formatters.discord_formatter import DiscordFormatter

        formatter = DiscordFormatter()

        assert formatter.format_toolcall_label("Bash", {"command": "rg *domain* src"}) == r"🔧 Bash: rg \*domain\* src"
        # Shortened before it is escaped, so the cut never leaves half an escape behind.
        long = formatter.format_toolcall_label("Bash", {"command": "a" * 60 + "*"}, max_len=20)
        assert long.endswith("…") and "\\" not in long
        # A code span is literal already; a backslash there would be shown.
        assert formatter.format_file_path("core/message_dispatcher.py") == "📁 File: `core/message_dispatcher.py`"
