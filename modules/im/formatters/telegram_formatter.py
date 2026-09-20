"""Telegram formatter with HTML rendering.

The rest of the app emits standard Markdown-ish text (`**bold**`, backticks,
links). Telegram is stricter than Slack/Discord, so we normalize that subset
into Bot API HTML before sending.
"""

from __future__ import annotations

import html
import re
import uuid

from .base_formatter import BaseMarkdownFormatter


class TelegramFormatter(BaseMarkdownFormatter):
    _CODE_BLOCK_RE = re.compile(r"```(?:([\w.+-]+)\n)?(.*?)```", re.DOTALL)
    _INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
    _INLINE_TOKENS = (("**", "b"), ("~~", "s"), ("*", "i"))

    def format_bold(self, text: str) -> str:
        return f"**{text}**"

    def format_italic(self, text: str) -> str:
        return f"*{text}*"

    def format_strikethrough(self, text: str) -> str:
        return f"~~{text}~~"

    def format_link(self, text: str, url: str) -> str:
        return f"[{text}]({url})"

    def escape_special_chars(self, text: str) -> str:
        return text

    @staticmethod
    def _find_link_url_end(text: str, start: int) -> int | None:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char.isspace():
                return None
            if char == "(":
                depth += 1
                continue
            if char != ")":
                continue
            if depth == 0:
                return index
            depth -= 1
        return None

    @staticmethod
    def _find_link_label_end(text: str, start: int) -> int | None:
        for index in range(start + 1, len(text)):
            char = text[index]
            if char == "[":
                return None
            if char == "]" and index + 1 < len(text) and text[index + 1] == "(":
                return index
            if char == "]":
                return None
        return None

    def _render_links(self, text: str) -> str:
        rendered_parts: list[str] = []
        cursor = 0

        while cursor < len(text):
            start = text.find("[", cursor)
            if start < 0:
                rendered_parts.append(self._apply_inline_formatting(text[cursor:]))
                break

            label_end = self._find_link_label_end(text, start)
            if label_end is None:
                rendered_parts.append(self._apply_inline_formatting(text[cursor : start + 1]))
                cursor = start + 1
                continue

            url_start = label_end + 2
            # A scheme is case-insensitive, and a link whose scheme is spelled
            # ``HTTPS://`` is still a link: missing it here left the raw
            # Markdown in the message instead of a hyperlink.
            if not text[url_start : url_start + 8].lower().startswith(
                ("http://", "https://")
            ):
                rendered_parts.append(self._apply_inline_formatting(text[cursor : start + 1]))
                cursor = start + 1
                continue

            url_end = self._find_link_url_end(text, url_start)
            if url_end is None:
                rendered_parts.append(self._apply_inline_formatting(text[cursor : start + 1]))
                cursor = start + 1
                continue

            rendered_parts.append(self._apply_inline_formatting(text[cursor:start]))
            rendered_parts.append(
                f'<a href="{text[url_start:url_end]}">{self._apply_inline_formatting(text[start + 1:label_end])}</a>'
            )
            cursor = url_end + 1

        return "".join(rendered_parts)

    def _render_inline_segment(self, text: str, start: int = 0, stop_token: str | None = None) -> tuple[str, int, bool]:
        rendered_parts: list[str] = []
        index = start

        while index < len(text):
            if stop_token and text.startswith(stop_token, index):
                return "".join(rendered_parts), index + len(stop_token), True

            matched_token = False
            for token, tag in self._INLINE_TOKENS:
                if not text.startswith(token, index):
                    continue
                inner, next_index, closed = self._render_inline_segment(text, index + len(token), token)
                if closed and inner:
                    rendered_parts.append(f"<{tag}>{inner}</{tag}>")
                    index = next_index
                else:
                    rendered_parts.append(token)
                    index += len(token)
                matched_token = True
                break

            if matched_token:
                continue

            rendered_parts.append(text[index])
            index += 1

        return "".join(rendered_parts), index, False

    def _apply_inline_formatting(self, text: str) -> str:
        rendered, _, _ = self._render_inline_segment(text)
        return rendered

    def render(self, text: str) -> str:
        if not text:
            return ""

        placeholders: dict[str, str] = {}

        def stash(replacement: str) -> str:
            token = f"\uE000TG{uuid.uuid4().hex}\uE001"
            placeholders[token] = replacement
            return token

        def render_code_block(match: re.Match[str]) -> str:
            language = (match.group(1) or "").strip()
            code = html.escape(match.group(2).strip("\n"))
            if language:
                return f'<pre><code class="language-{html.escape(language, quote=True)}">{code}</code></pre>'
            return f"<pre><code>{code}</code></pre>"

        def render_inline_code(match: re.Match[str]) -> str:
            return f"<code>{html.escape(match.group(1))}</code>"

        rendered = self._CODE_BLOCK_RE.sub(lambda m: stash(render_code_block(m)), text)
        rendered = self._INLINE_CODE_RE.sub(lambda m: stash(render_inline_code(m)), rendered)
        rendered = html.escape(rendered)

        rendered = self._render_links(rendered)

        for token, replacement in placeholders.items():
            rendered = rendered.replace(token, replacement)
        return rendered
