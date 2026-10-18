from .base_formatter import BaseMarkdownFormatter


class FeishuFormatter(BaseMarkdownFormatter):
    """Feishu/Lark markdown formatter

    Feishu supports a subset of standard Markdown in rich text messages.
    Reference: https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json
    """

    def format_bold(self, text: str) -> str:
        return f"**{text}**"

    def format_italic(self, text: str) -> str:
        return f"*{text}*"

    def format_strikethrough(self, text: str) -> str:
        return f"~~{text}~~"

    def format_link(self, text: str, url: str) -> str:
        return f"[{text}]({url})"

    def format_code_inline(self, text: str) -> str:
        return f"`{text}`"

    def format_code_block(self, code: str, language: str = "") -> str:
        return f"```{language}\n{code}\n```"

    def format_quote(self, text: str) -> str:
        lines = text.split("\n")
        return "\n".join(f"> {line}" for line in lines)

    def format_list_item(self, text: str, level: int = 0) -> str:
        indent = "  " * level
        return f"{indent}- {text}"

    def format_numbered_list_item(self, text: str, number: int, level: int = 0) -> str:
        indent = "  " * level
        return f"{indent}{number}. {text}"
