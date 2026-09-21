from core.citations import spell_destination

from .base_formatter import BaseMarkdownFormatter


def encode_slack_delimiters(text: str) -> str:
    """Spell ``&``, ``<`` and ``>`` so Slack shows them instead of reading them.

    For text whose Markdown has already been interpreted - a link label, whose
    backslash escapes have been restored and whose character references have
    been resolved, so every one of these three characters left in it is one
    the reader is meant to see. Slack reads all three as markup of its own, and
    inside ``<url|label>`` a bare ``>`` ends the link early, so each is encoded
    here, once and unconditionally.

    Encoding every one of them is what keeps the two spellings apart. A label
    that MEANS ``&`` arrives here as ``&``; a label that SPELLS ``&amp;``
    arrives as those five characters and has to leave as ``&amp;amp;`` to be
    shown as five. Leaving an already-spelled reference alone would deliver one
    string for both, which is the caller's distinction to keep, not this
    function's to guess from the finished text.

    Not the same job as ``escape_special_chars``, which encodes text that was
    never Markdown (a path, a tool name) - the same encoding, reached without
    any Markdown interpretation in between.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def spell_uri_escapes(url: str) -> str:
    """Write a resolved destination the way its Markdown consumer spells it.

    Not an encoder for a URL that was never one, and not a canonicaliser: this
    is the address a Markdown reader already resolved, on its way into a place
    that cannot hold every character. ``spell_uri`` is the rule the renderer on
    the other side of that boundary applies to the same destination, so the two
    write the same address rather than two spellings nobody has checked name
    one page. A ``%3E`` that arrived spelled stays spelled once; a ``%`` that
    starts no escape is data and is written ``%25``, because that is what the
    consumer reads it as.

    What it buys inside ``<url|label>`` falls out of the same rule. ``>`` ends
    the link early and ``|`` starts the label early, so a destination holding
    either arrives truncated with the rest shown as plain text; both are
    outside the safe set and leave as ``%3E`` and ``%7C``. So do a space, a
    backslash, a backtick and every C0 control - including the newline a
    character reference can put in an address, which no single-line wrapper
    survives.

    A bracketed IPv6 authority is the one place an escape has to come back:
    those two brackets are the host's own syntax rather than data, and nothing
    opens ``%5B::1%5D``. Which is why the destination is handed over unspelled
    and ``spell_destination`` does both halves - once spelled, a host written
    ``[::1]`` and a provider's own ``%5B::1%5D`` are the same characters, and
    putting brackets back into the second one would deliver a live link to an
    address the provider never named.
    """
    return spell_destination(url)


class SlackFormatter(BaseMarkdownFormatter):
    """Slack mrkdwn formatter
    
    Slack uses its own markup language called mrkdwn which is similar to Markdown
    but with some differences.
    Reference: https://api.slack.com/reference/surfaces/formatting
    """
    
    def format_bold(self, text: str) -> str:
        """Format bold text using single asterisks"""
        # In Slack, single asterisks make text bold
        return f"*{text}*"
    
    def format_italic(self, text: str) -> str:
        """Format italic text using underscores"""
        return f"_{text}_"
    
    def format_strikethrough(self, text: str) -> str:
        """Format strikethrough text using tildes"""
        return f"~{text}~"
    
    def format_link(self, text: str, url: str) -> str:
        """Format hyperlink in Slack style"""
        # Slack uses a different link format: <url|text>
        return f"<{url}|{text}>"
    
    def escape_special_chars(self, text: str) -> str:
        """Escape Slack mrkdwn special characters
        
        Slack requires escaping these characters: &, <, >
        """
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        return text
    
    def format_code_inline(self, text: str) -> str:
        """Format inline code - no escaping inside code blocks"""
        # In Slack, content inside backticks should NOT be escaped
        # Special characters like . and | are literal inside code blocks
        return f"`{text}`"
    
    def format_code_block(self, code: str, language: str = "") -> str:
        """Format code block - Slack style"""
        # Slack also supports triple backticks for code blocks
        # Language hint is not displayed but can be included
        return f"```\n{code}\n```"
    
    def format_quote(self, text: str) -> str:
        """Format quoted text - Slack style"""
        # Slack uses > for quotes, same as standard markdown
        lines = text.split('\n')
        return '\n'.join(f">{line}" for line in lines)
    
    def format_list_item(self, text: str, level: int = 0) -> str:
        """Format list item - Slack style"""
        # Slack doesn't support nested lists well, so we simplify
        if level == 0:
            return f"• {text}"
        else:
            # Use spaces for indentation
            indent = "  " * level
            return f"{indent}◦ {text}"
    
    def format_numbered_list_item(self, text: str, number: int, level: int = 0) -> str:
        """Format numbered list item - Slack style"""
        indent = "  " * level
        return f"{indent}{number}. {text}"
    
    # Slack-specific formatting methods
    def format_user_mention(self, user_id: str) -> str:
        """Format user mention in Slack style"""
        return f"<@{user_id}>"
    
    def format_channel_mention(self, channel_id: str) -> str:
        """Format channel mention in Slack style"""
        return f"<#{channel_id}>"
    
    def format_emoji(self, emoji: str, name: str = None) -> str:
        """Format emoji - Slack supports both Unicode and :name: format"""
        if name:
            return f":{name}:"
        return emoji
    
    # Override convenience methods for Slack-specific behavior
    def format_section_header(self, title: str, emoji: str = "") -> str:
        """Format section header - Slack doesn't have headers, so we use bold"""
        if emoji:
            return f"{emoji} {self.format_bold(title)}"
        return self.format_bold(title)
    
    def format_key_value(self, key: str, value: str, inline: bool = True) -> str:
        """Format key-value pair - override to avoid double escaping"""
        # Only escape the value, not the key (key is bolded)
        escaped_value = self.escape_special_chars(value)
        
        if inline:
            return f"{self.format_bold(key)}: {escaped_value}"
        else:
            return f"{self.format_bold(key)}:\n{escaped_value}"
    
    def format_horizontal_rule(self) -> str:
        """Format horizontal rule - Slack doesn't support this well"""
        # Use a series of dashes as a visual separator
        return "─" * 40
    
    def format_tool_name(self, tool_name: str, emoji: str = "🔧") -> str:
        """Format tool name with emoji and styling"""
        # Don't escape tool name - it goes directly in code blocks
        return f"{emoji} {self.format_bold('Tool')}: {self.format_code_inline(tool_name)}"
    
    def format_file_path(self, path: str, emoji: str = "📁") -> str:
        """Format file path with emoji"""
        # Don't escape path - it goes directly in code blocks
        return f"{emoji} File: {self.format_code_inline(path)}"
    
    def format_command(self, command: str) -> str:
        """Format shell command"""
        # For multi-line or long commands, use code block
        if "\n" in command or len(command) > 80:
            return f"💻 Command:\n{self.format_code_block(command)}"
        else:
            # Don't escape command - it goes directly in code blocks
            return f"💻 Command: {self.format_code_inline(command)}"