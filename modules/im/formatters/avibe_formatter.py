"""Avibe (Web UI) markdown formatter.

The Vibe Remote workbench renders agent output as CommonMark + GFM in
the browser, so the formatter uses standard markdown syntax for every
operation. Mirrors ``DiscordFormatter`` — both render through a
CommonMark pipeline.
"""

from .base_formatter import BaseMarkdownFormatter


class AvibeFormatter(BaseMarkdownFormatter):
    """Standard CommonMark + GFM formatter for the Web UI."""

    def format_bold(self, text: str) -> str:
        return f"**{text}**"

    def format_italic(self, text: str) -> str:
        return f"*{text}*"

    def format_strikethrough(self, text: str) -> str:
        return f"~~{text}~~"

    def format_link(self, text: str, url: str) -> str:
        return f"[{text}]({url})"

    def format_code_block(self, code: str, language: str = "") -> str:
        if language:
            return f"```{language}\n{code}\n```"
        return f"```\n{code}\n```"
