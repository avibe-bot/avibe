"""WeChat markdown formatter.

WeChat personal messages support only plain text — no bold, italic,
strikethrough, or clickable hyperlinks.  This formatter passes text
through as-is and renders links as ``text (url)`` so the URL is still
visible to the user.
"""

from .base_formatter import BaseMarkdownFormatter


class WeChatFormatter(BaseMarkdownFormatter):
    """WeChat plain-text formatter.

    WeChat personal chat does not support rich markdown formatting,
    so all style methods return undecorated text.  Links are shown
    inline as ``text (url)`` since clickable hyperlinks are not
    available.
    """

    def format_bold(self, text: str) -> str:
        return text

    def format_italic(self, text: str) -> str:
        return text

    def format_strikethrough(self, text: str) -> str:
        return text

    def format_link(self, text: str, url: str) -> str:
        return f"{text} ({url})"

    def format_code_inline(self, text: str) -> str:
        # No inline code styling in plain text; return as-is
        return text

    def format_code_block(self, code: str, language: str = "") -> str:
        # Render code blocks as plain indented text
        return code

    def format_quote(self, text: str) -> str:
        lines = text.split("\n")
        return "\n".join(f"> {line}" for line in lines)

    def format_list_item(self, text: str, level: int = 0) -> str:
        indent = "  " * level
        return f"{indent}- {text}"

    def format_numbered_list_item(self, text: str, number: int, level: int = 0) -> str:
        indent = "  " * level
        return f"{indent}{number}. {text}"
