from .base_formatter import BaseMarkdownFormatter


class DiscordFormatter(BaseMarkdownFormatter):
    """Discord markdown formatter.

    Discord supports standard Markdown with a few extensions.
    Reference: https://support.discord.com/hc/en-us/articles/210298617
    """

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
