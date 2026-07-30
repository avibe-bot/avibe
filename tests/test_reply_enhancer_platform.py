import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.reply_enhancer as reply_enhancer
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.reply_enhancer import process_reply, strip_silent_blocks
from core.system_prompt_injection import build_system_prompt_injection
from config import paths
from modules.im import MessageContext


class _StubSettingsManager:
    @staticmethod
    def _canonicalize_message_type(message_type: str) -> str:
        return message_type

    @staticmethod
    def is_message_type_hidden(settings_key: str, message_type: str) -> bool:
        return False


class _StubIMClient:
    def __init__(self):
        self.sent_messages = []
        self.sent_button_messages = []
        self.uploaded_markdowns = []
        self._next_id = 1

    @staticmethod
    def should_use_thread_for_reply() -> bool:
        return False

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent_messages.append((context.channel_id, text, parse_mode))
        message_id = f"msg-{self._next_id}"
        self._next_id += 1
        return message_id

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        self.sent_button_messages.append((context.channel_id, text, parse_mode, keyboard))
        message_id = f"btn-{self._next_id}"
        self._next_id += 1
        return message_id

    async def upload_markdown(self, context, title, content, filetype="markdown"):
        self.uploaded_markdowns.append((context.channel_id, title, content, filetype))
        return "file-1"


class _StubController:
    def __init__(self, platform: str, progress_style: str = "verbose"):
        self.config = type(
            "Config",
            (),
            {"platform": platform, "reply_enhancements": True},
        )()
        self.settings_manager = _StubSettingsManager()
        self.im_client = _StubIMClient()
        # Process/assistant messages are gated by the progress style on
        # status-bubble platforms (Slack/Discord/Lark). These content-transform
        # tests exercise the verbose log-message delivery path, so default to
        # "verbose" (lark's historical effective behavior before it gained the
        # status-bubble capability).
        self._progress_style = progress_style

    def get_progress_style_for_context(self, context=None) -> str:
        return self._progress_style

    @staticmethod
    def _get_settings_key(context: MessageContext) -> str:
        return f"{context.channel_id}:{context.user_id}"

    @staticmethod
    def _get_session_key(context: MessageContext) -> str:
        return f"{getattr(context, 'platform', None) or 'test'}::{context.channel_id}:{context.user_id}"

    def get_settings_manager_for_context(self, context=None):
        return self.settings_manager


class ReplyEnhancerPlatformTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_can_exclude_quick_replies(self):
        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            prompt = build_system_prompt_injection(include_quick_replies=False)

        self.assertIn("## Silent replies", prompt)
        self.assertIn("<silent>reason not shown to the user</silent>", prompt)
        self.assertIn(
            "If the user asks you to configure, repair, or operate Avibe itself, read `https://github.com/avibe-bot/avibe/raw/master/skills/use-avibe/SKILL.md` before making changes.",
            prompt,
        )
        self.assertIn("## Send files", prompt)
        self.assertIn("Avibe provides optional capabilities:", prompt)
        self.assertNotIn("If you generate an image with Codex", prompt)
        self.assertNotIn("## Quick-reply buttons", prompt)
        self.assertIn("## Memory and Project Context", prompt)
        self.assertIn("`/tmp/user_preferences.md`", prompt)
        self.assertIn("Use the current platform `<platform>`", prompt)
        self.assertIn("`<platform>/<user_id>`", prompt)

    def test_prompt_can_include_codex_generated_image_instructions(self):
        with (
            patch.dict(os.environ, {"CODEX_HOME": "/Users/test/.codex"}),
            patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")),
        ):
            prompt = build_system_prompt_injection(
                include_quick_replies=False,
                include_codex_generated_images=True,
            )

        self.assertIn("### Codex-generated images", prompt)
        self.assertIn("If you generate an image with Codex", prompt)
        self.assertIn("file:///Users/test/.codex/generated_images/thread-id/image-file.png", prompt)
        self.assertIn("Never emit variables, placeholder paths, or sandbox paths like `/mnt/data/...`", prompt)

    def test_prompt_includes_vault_guidance(self):
        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            prompt = build_system_prompt_injection(include_quick_replies=False)

        self.assertIn("## Vault", prompt)
        self.assertIn("prefer Avibe Vault: agents reference secrets by name, tag, or skill tag", prompt)
        self.assertIn("Static secret: a regular secret value", prompt)
        self.assertIn("Keypair secret: a signing key", prompt)
        self.assertIn("Because protected secrets are end-to-end encrypted", prompt)
        self.assertNotIn("irreversible operations", prompt)
        self.assertIn("never run commands that may print env vars", prompt)
        self.assertNotIn("With `vibe vault fetch` and `vibe vault sign`, the agent does not receive", prompt)
        self.assertIn("Avibe automatically asks the user to decrypt and authorize access", prompt)
        self.assertIn("it does not replay the command for you", prompt)
        self.assertIn("run the same `run` / `fetch` command again", prompt)
        self.assertIn("Avibe creates a browser signing request and returns immediately", prompt)
        self.assertIn("Do not rerun `sign`", prompt)
        self.assertIn("follow the callback instruction to read the completed request result", prompt)
        self.assertIn("vibe vault request OPENAI_API_KEY", prompt)
        self.assertIn("Request that the user add a missing static secret.", prompt)
        self.assertIn("ask the user to create a keypair secret in the Vault UI", prompt)
        self.assertIn("do not request or store private-key material as a static secret", prompt)
        self.assertNotIn("$<OPENAI_API_KEY>", prompt)
        self.assertNotIn("clickable placeholder", prompt)
        self.assertIn("vibe vault find --kind static --protection protected", prompt)
        self.assertIn("vibe vault find openai --tag prod", prompt)
        self.assertIn("vibe vault tags", prompt)
        self.assertNotIn("vibe vault discover", prompt)
        self.assertIn("vibe vault run --env OPENAI_API_KEY,GITHUB_TOKEN", prompt)
        self.assertIn("vibe vault run --tag deploy", prompt)
        self.assertIn("vibe vault fetch --auth GITHUB_PAT", prompt)
        self.assertIn("vibe vault access PROD_DB_URL", prompt)
        self.assertIn("Request approval before a protected `run`", prompt)
        self.assertIn("For protected `fetch`, run `vibe vault fetch`", prompt)
        self.assertIn("vibe vault sign WALLET_KEY", prompt)
        self.assertNotIn("vibe vault await <request_id>", prompt)
        self.assertNotIn("vibe vault sign WALLET_KEY --skill", prompt)
        self.assertNotIn("vibe vault sign WALLET_KEY --tag", prompt)

    def test_prompt_includes_vault_web_placeholder_only_for_web_chat(self):
        web_context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )
        slack_context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            web_prompt = build_system_prompt_injection(include_quick_replies=False, context=web_context)
            slack_prompt = build_system_prompt_injection(include_quick_replies=False, context=slack_context)

        self.assertIn("$<OPENAI_API_KEY>", web_prompt)
        self.assertIn("clickable placeholder in your reply", web_prompt)
        self.assertNotIn("$<OPENAI_API_KEY>", slack_prompt)
        self.assertNotIn("clickable placeholder in your reply", slack_prompt)

    def test_prompt_can_exclude_show_pages(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            prompt = build_system_prompt_injection(
                include_show_pages=False,
                include_quick_replies=False,
                context=context,
            )

        self.assertNotIn("## Show Pages", prompt)
        self.assertIn("## Harness", prompt)
        self.assertNotIn("## Scheduled tasks, watches, and hooks", prompt)
        self.assertIn("Current session id: `sesk8m4q2p7x`", prompt)

    def test_prompt_can_exclude_user_preferences(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            prompt = build_system_prompt_injection(
                include_quick_replies=False,
                include_user_preferences=False,
                context=context,
            )

        self.assertIn("Current session id: `sesk8m4q2p7x`", prompt)
        self.assertNotIn("## Memory and Project Context", prompt)
        self.assertNotIn("/tmp/user_preferences.md", prompt)
        self.assertNotIn("slack/U1", prompt)

    def test_process_reply_strips_silent_blocks_before_enhancements(self):
        reply = process_reply(
            "Visible\n<silent>skip [secret](file:///tmp/secret.txt)\n---\n[Hidden]</silent>\nDone"
        )

        self.assertEqual(reply.text, "Visible\n\nDone")
        self.assertEqual(reply.files, [])
        self.assertEqual(reply.buttons, [])

    def test_process_reply_preserves_silent_file_link_literal_inside_code(self):
        specimens = [
            "Example `<silent>[file](file:///tmp/secret.txt)</silent>` remains.",
            "```markdown\n<silent>[file](file:///tmp/secret.txt)</silent>\n```",
            "```markdown\n> <silent>[file](file:///tmp/secret.txt)</silent>\n```",
        ]

        for text in specimens:
            with self.subTest(text=text):
                reply = process_reply(text)
                self.assertEqual(reply.text, text)
                self.assertEqual(reply.files, [])

    def test_process_reply_only_extracts_file_links_outside_markdown_code(self):
        text = (
            "Attach [report](file:///tmp/report.txt); preserve "
            "`<silent>[example](file:///tmp/secret.txt)</silent>`."
        )

        reply = process_reply(text)

        self.assertEqual(
            reply.text,
            "Attach report; preserve "
            "`<silent>[example](file:///tmp/secret.txt)</silent>`.",
        )
        self.assertEqual([file.path for file in reply.files], ["/tmp/report.txt"])

    def test_process_reply_recovers_attachment_label_from_original_text(self):
        reply = process_reply("[see `report`](file:///tmp/report.txt)")

        self.assertEqual(reply.text, "see `report`")
        self.assertEqual([file.label for file in reply.files], ["see `report`"])

    def test_process_reply_uses_shared_code_mask_for_secret_requests(self):
        text = (
            "````markdown\n"
            "text ``` inner\n"
            "<silent>$<OPENAI_KEY></silent>\n"
            "````"
        )

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.secret_requests, [])

    def test_process_reply_uses_shared_code_mask_for_quick_replies(self):
        text = (
            "```markdown\n"
            "<silent>literal</silent>\n"
            "---\n"
            "[Yes] | [No]"
        )

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.buttons, [])

    def test_silent_parser_preserves_inline_code_and_trailing_report_byte_for_byte(self):
        trailing_report = "\n".join(
            [
                "2. Keep queue and cancellation work in their existing lanes.",
                "3. Persist the complete terminal message and callback payload.",
                "4. Re-run the exact-head review and CI gates.",
            ]
        )
        text = (
            "Intermediate assistant text must not leave its Session; "
            "the literal directive is `<silent>`.\n\n"
            f"{trailing_report}"
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_preserves_fenced_code_byte_for_byte(self):
        text = (
            "Examples:\n\n"
            "```markdown\n"
            "<silent>complete example</silent>\n"
            "<silent>\n"
            "substantial text after the unmatched literal\n"
            "```\n\n"
            "~~~text\n"
            "<SILENT data-example=\"true\">\n"
            "more literal text\n"
            "~~~\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_preserves_container_fences_byte_for_byte(self):
        text = (
            "- ```text\n"
            "  <silent>list literal</silent>\n"
            "  ```\n\n"
            "> ~~~text\n"
            "> <silent>quote literal</silent>\n"
            "> ~~~\n\n"
            "> - ````markdown\n"
            ">   ```nested```\n"
            ">   <silent>nested literal</silent>\n"
            ">   ````\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_preserves_tab_indented_list_fence(self):
        text = (
            "-\t```text\n"
            "\t<silent>tab-indented literal</silent>\n"
            "\t```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_preserves_continuation_line_list_fence(self):
        text = (
            "10. Example\n"
            "    ```text\n"
            "    <silent>continuation literal</silent>\n"
            "    ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_retains_list_state_across_lazy_continuation(self):
        text = (
            "- item\n"
            "lazy continuation\n"
            "    ```text\n"
            "    <silent>lazy list literal</silent>\n"
            "    ```"
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_tracks_nested_list_context_across_lines(self):
        text = (
            "- Outer\n"
            "  1. Inner\n"
            "     ```text\n"
            "     <silent>nested continuation literal</silent>\n"
            "     ```\n"
            "  ```text\n"
            "  > <silent>outer continuation literal</silent>\n"
            "  ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_tracks_empty_ordered_list_item(self):
        text = (
            "10.\n"
            "    ```text\n"
            "    <silent>empty-item continuation literal</silent>\n"
            "    ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_tracks_quote_nested_inside_list(self):
        text = (
            "- > ```text\n"
            "  > <silent>list quote literal</silent>\n"
            "  > ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_enforces_list_indent_before_nested_quote(self):
        text = (
            "- > ```text\n"
            "> <silent>remove outside code</silent>\n"
            "> ```"
        )

        self.assertEqual(strip_silent_blocks(text), "- > ```text\n> \n> ```")

    def test_silent_parser_tracks_alternating_quote_and_list_containers(self):
        text = (
            "> - > - ```text\n"
            ">   >   <silent>alternating container literal</silent>\n"
            ">   >   ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_preserves_inline_code_after_non_one_list_text(self):
        text = (
            "Paragraph\n"
            "10. Not a list interruption\n"
            "    ```text\n"
            "    <silent>paragraph code literal</silent>\n"
            "    ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_rejects_empty_list_interrupting_paragraph(self):
        text = (
            "Paragraph\n"
            "-\n"
            "    ```text\n"
            "  <silent>remove outside code</silent>\n"
            "    ```\n"
            "Tail remains."
        )
        expected = (
            "Paragraph\n"
            "-\n"
            "    ```text\n"
            "  \n"
            "    ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), expected)

    def test_silent_parser_uses_list_relative_indented_code_threshold(self):
        text = (
            "- item\n"
            "\n"
            "    <silent>remove list paragraph content</silent>\n"
            "\n"
            "      <silent>preserve list indented code</silent>"
        )
        expected = (
            "- item\n"
            "\n"
            "    \n"
            "\n"
            "      <silent>preserve list indented code</silent>"
        )

        self.assertEqual(strip_silent_blocks(text), expected)

    def test_silent_parser_uses_nested_list_indented_code_threshold(self):
        text = (
            "- outer\n"
            "  - middle\n"
            "    - inner\n"
            "\n"
            "        <silent>remove inner list content</silent>"
        )

        self.assertEqual(
            strip_silent_blocks(text),
            "- outer\n  - middle\n    - inner",
        )

    def test_silent_parser_preserves_alternating_container_indented_code(self):
        text = "- >     <silent>alternating indented literal</silent>"

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_resets_paragraph_state_after_heading(self):
        text = (
            "# Heading\n"
            "10. item\n"
            "    ```text\n"
            "    <silent>heading list literal</silent>\n"
            "    ```\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_preserves_inline_code_after_orphan_setext_marker(self):
        text = (
            "===\n"
            "10. item\n"
            "    ```text\n"
            "    <silent>paragraph code literal</silent>\n"
            "    ```"
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_keeps_invalid_fence_line_in_paragraph_state(self):
        text = (
            "``` foo`bar\n"
            "10. item\n"
            "    ```text\n"
            "    <silent>remove outside code</silent>\n"
            "    ```"
        )

        self.assertEqual(
            strip_silent_blocks(text),
            "``` foo`bar\n10. item\n    ```text\n    \n    ```",
        )

    def test_silent_parser_allows_list_after_link_reference_definition(self):
        text = (
            "[ref]: /url\n"
            "10. item\n"
            "    ```text\n"
            "    <silent>link definition literal</silent>\n"
            "    ```"
        )

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_stops_unclosed_fence_at_container_boundary(self):
        text = (
            "- ```text\n"
            "  <silent>list literal</silent>\n"
            "List ended.\n"
            "<silent>remove after list</silent>\n\n"
            "> ~~~text\n"
            "> <silent>quote literal</silent>\n"
            "Quote ended.\n"
            "<silent>remove after quote</silent>\n"
            "Tail remains."
        )
        expected = (
            "- ```text\n"
            "  <silent>list literal</silent>\n"
            "List ended.\n"
            "\n\n"
            "> ~~~text\n"
            "> <silent>quote literal</silent>\n"
            "Quote ended.\n"
            "\n"
            "Tail remains."
        )

        self.assertEqual(strip_silent_blocks(text), expected)

    def test_silent_parser_removes_real_blocks_while_preserving_code_examples(self):
        text = (
            "Start\n"
            "<silent>remove one</silent>\n"
            "Inline `<silent>literal one</silent>` stays.\n"
            "<SILENT reason=\"hidden\">remove two</silent >\n"
            "````markdown\n"
            "```nested fence```\n"
            "<silent>literal two</silent>\n"
            "````\n"
            "End"
        )
        expected = (
            "Start\n"
            "\n"
            "Inline `<silent>literal one</silent>` stays.\n"
            "\n"
            "````markdown\n"
            "```nested fence```\n"
            "<silent>literal two</silent>\n"
            "````\n"
            "End"
        )

        self.assertEqual(strip_silent_blocks(text), expected)

    def test_silent_parser_handles_backtick_and_escape_edges(self):
        protected = "Double ``literal `<silent>` marker`` remains."
        escaped = r"Before \`<silent>remove me</silent>\` after"
        partial_escape = "\\``<silent>literal</silent>`"
        escaped_closer = r"`<silent>literal</silent>\`"

        self.assertEqual(strip_silent_blocks(protected), protected)
        self.assertEqual(strip_silent_blocks(escaped), r"Before \`\` after")
        self.assertEqual(strip_silent_blocks(partial_escape), partial_escape)
        self.assertEqual(strip_silent_blocks(escaped_closer), escaped_closer)

    def test_silent_parser_preserves_multiline_inline_code_span(self):
        text = "`foo\n<silent>multiline literal</silent>\nbar`\nTail remains."

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_ignores_backticks_inside_raw_html_tags(self):
        text = '<a title="`">x</a> <silent>remove me</silent> `tail'

        self.assertEqual(
            strip_silent_blocks(text),
            '<a title="`">x</a>  `tail',
        )

    def test_silent_parser_rejects_invalid_raw_html_tag_grammar(self):
        text = '<a? title="`"> [literal](file:///tmp/secret.txt) `'

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.files, [])

    def test_silent_parser_honors_escape_before_raw_html_candidate(self):
        text = r'\<a title="`"> [literal](file:///tmp/secret.txt) `'

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.files, [])

    def test_silent_parser_rejects_unicode_space_in_raw_html_tag(self):
        text = '<a\u00a0title="`"> [literal](file:///tmp/secret.txt) `'

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.files, [])

    def test_silent_parser_rejects_attributes_on_raw_html_closing_tags(self):
        text = '</a title="`"> [literal](file:///tmp/secret.txt) `'

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.files, [])

    def test_silent_parser_parses_html_as_text_inside_open_code_span(self):
        text = (
            '`prefix <a title="`"> '
            "<silent>remove outside code</silent> "
            "`tail`"
        )

        self.assertEqual(
            strip_silent_blocks(text),
            '`prefix <a title="`">  `tail`',
        )

    def test_silent_parser_ignores_backticks_inside_uri_autolinks(self):
        text = (
            "<https://example.com/`> "
            "<silent>remove outside code</silent> "
            "`tail"
        )

        self.assertEqual(
            strip_silent_blocks(text),
            "<https://example.com/`>  `tail",
        )

    def test_silent_parser_strips_quote_prefixes_before_inline_html(self):
        text = (
            "> text <!A\n"
            "> `> <silent>remove outside code</silent> `tail"
        )

        self.assertEqual(
            strip_silent_blocks(text),
            "> text <!A\n> `>  `tail",
        )

    def test_silent_parser_maps_normalized_nul_to_original_source(self):
        text = (
            "`before\x00"
            "[literal](file:///tmp/secret.txt) "
            "<silent>literal</silent>"
            "after`"
        )

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.files, [])

    def test_silent_parser_maps_tab_expanded_list_continuation(self):
        text = (
            "- item\n"
            "\t`<silent>[literal](file:///tmp/secret.txt)</silent>`"
        )

        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.files, [])

    def test_silent_parser_handles_many_unmatched_backtick_runs(self):
        unmatched_runs = " ".join("`" * length for length in range(2, 502))
        text = f"Literal `<silent>` remains.\n{unmatched_runs}\nTail remains."

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_does_not_pair_inline_spans_across_lines(self):
        text = "    ```\n<silent>remove outside code</silent>\n    ```\nTail remains."

        self.assertEqual(
            strip_silent_blocks(text),
            "    ```\n\n    ```\nTail remains.",
        )

    def test_silent_parser_ends_quoted_fence_at_unmarked_blank_line(self):
        text = (
            "> ```text\n"
            "\n"
            "> <silent>remove outside code</silent>\n"
            "> ```"
        )

        self.assertEqual(strip_silent_blocks(text), "> ```text\n\n> \n> ```")

    def test_silent_parser_preserves_indented_code_literal(self):
        text = "Example:\n\n    <silent>indented literal</silent>\nTail remains."

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_preserves_quoted_indented_code_literal(self):
        text = ">     <silent>quoted indented literal</silent>"

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_ignores_fences_inside_raw_html_blocks(self):
        text = (
            "<pre>\n"
            "```\n"
            "<silent>remove real directive</silent>\n"
            "```\n"
            "</pre>\n"
            "Tail remains."
        )
        expected = "<pre>\n```\n\n```\n</pre>\nTail remains."

        self.assertEqual(strip_silent_blocks(text), expected)

    def test_silent_parser_does_not_mask_indented_content_in_raw_html_block(self):
        text = (
            "<pre>\n"
            "\n"
            "    <silent>remove real directive</silent>\n"
            "</pre>"
        )

        self.assertEqual(strip_silent_blocks(text), "<pre>\n\n    \n</pre>")

    def test_silent_parser_ignores_fences_inside_html_comment_blocks(self):
        text = (
            "<!--\n"
            "```\n"
            "-->\n"
            "<silent>remove real directive</silent>\n"
            "```"
        )

        self.assertEqual(strip_silent_blocks(text), "<!--\n```\n-->\n\n```")

    def test_silent_parser_does_not_parse_code_spans_in_unterminated_html_blocks(self):
        for opener in ("<!--", "<?target", "<![CDATA[", "<!DOCTYPE"):
            with self.subTest(opener=opener):
                text = f"{opener}\n`<silent>hidden</silent>`"

                self.assertEqual(strip_silent_blocks(text), f"{opener}\n``")

    def test_process_reply_keeps_fence_newline_before_quick_replies(self):
        text = "```text\nexample\n```\n---\n[Yes] | [No]"

        reply = process_reply(text)

        self.assertEqual(reply.text, "```text\nexample\n```")
        self.assertEqual([button.text for button in reply.buttons], ["Yes", "No"])

    def test_silent_parser_removes_many_control_blocks_in_one_pass(self):
        blocks = "".join(
            f"visible-{index}<silent>hidden-{index}</silent>"
            for index in range(2_000)
        )
        expected = "".join(f"visible-{index}" for index in range(2_000))

        self.assertEqual(strip_silent_blocks(blocks), expected)

    def test_silent_parser_parses_progressively_exposed_blocks_once(self):
        lengths = range(1, 201)
        blocks = "".join(
            f"<silent>{'`' * length}</silent>" for length in lengths
        )
        closers = " ".join("`" * length for length in lengths)
        text = f"prefix {blocks} {closers}"

        with patch.object(
            reply_enhancer._BLOCK_MARKDOWN,
            "parse",
            wraps=reply_enhancer._BLOCK_MARKDOWN.parse,
        ) as parse:
            result = strip_silent_blocks(text)

        self.assertEqual(result, f"prefix  {closers}")
        self.assertEqual(parse.call_count, 1)

    def test_silent_parser_handles_many_malformed_html_prefixes_linearly(self):
        text = "<a" * 8_000 + " `<silent>literal</silent>`"

        self.assertEqual(strip_silent_blocks(text), text)

    def test_silent_parser_strips_many_malformed_html_comments_linearly(self):
        text = "<!--" * 8_000 + " `<silent>literal</silent>`"

        self.assertEqual(strip_silent_blocks(text), "<!--" * 8_000 + " ``")

    def test_reply_enhancer_treats_unicode_digits_as_plain_text(self):
        text = "². item\n①. item"

        self.assertEqual(process_reply(text).text, text)

    def test_silent_parser_does_not_pair_inline_spans_across_cr_lines(self):
        text = "    ```\r<silent>remove outside code</silent>\r    ```\rTail remains."

        self.assertEqual(
            strip_silent_blocks(text),
            "    ```\r\r    ```\rTail remains.",
        )

    def test_silent_parser_closes_real_block_before_parsing_hidden_markdown(self):
        text = "Before\n<silent>\n```\nhidden\n</silent>\nTail remains."

        self.assertEqual(strip_silent_blocks(text), "Before\n\nTail remains.")

    def test_silent_parser_keeps_unterminated_recovery_outside_code(self):
        text = "Visible result\n<silent>unfinished hidden diagnostic\nmust not leak"

        self.assertEqual(strip_silent_blocks(text), "Visible result")

    def test_silent_parser_keeps_all_silent_response_empty(self):
        self.assertEqual(strip_silent_blocks("<silent>internal only</silent>"), "")

    def test_silent_parser_preserves_boundary_indented_code(self):
        text = (
            "<silent>remove real directive</silent>\n\n"
            "    <silent>[literal](file:///tmp/secret.txt)</silent>"
        )

        reply = process_reply(text)

        self.assertEqual(
            reply.text,
            "    <silent>[literal](file:///tmp/secret.txt)</silent>",
        )
        self.assertEqual(reply.files, [])

    def test_silent_parser_does_not_reparse_synthesized_openers(self):
        text = (
            "Visible <sil"
            "<silent>hidden</silent>"
            "ent>KEEP</silent> tail"
        )

        self.assertEqual(
            strip_silent_blocks(text),
            "Visible <silent>KEEP</silent> tail",
        )

    def test_process_reply_can_disable_quick_reply_button_parsing_only(self):
        reply = process_reply(
            "Report [file](file:///tmp/report.txt)\n\n---\n[Continue] | [Stop]",
            include_quick_replies=False,
        )

        self.assertEqual(reply.text, "Report file\n\n---\n[Continue] | [Stop]")
        self.assertEqual([file.path for file in reply.files], ["/tmp/report.txt"])
        self.assertEqual(reply.buttons, [])

    def test_process_reply_accepts_markdown_link_style_quick_reply_button(self):
        reply = process_reply(
            "Done.\n\n---\n"
            "[:eyes: 看 PR](<https://github.com/avibe-bot/avibe/pull/298>) | "
            "[:rocket: 等评审完合并] | [:test_tube: 先回归测一遍]"
        )

        self.assertEqual(reply.text, "Done.")
        self.assertEqual(
            [button.text for button in reply.buttons],
            [":eyes: 看 PR", ":rocket: 等评审完合并", ":test_tube: 先回归测一遍"],
        )

    def test_process_reply_accepts_slack_angle_link_style_quick_reply_button(self):
        reply = process_reply(
            "Done.\n\n---\n"
            "<https://github.com/avibe-bot/avibe/pull/298|:eyes: 看 PR> | "
            "[:rocket: 等评审完合并] | [:test_tube: 先回归测一遍]"
        )

        self.assertEqual(reply.text, "Done.")
        self.assertEqual(
            [button.text for button in reply.buttons],
            [":eyes: 看 PR", ":rocket: 等评审完合并", ":test_tube: 先回归测一遍"],
        )

    def test_process_reply_ignores_bare_angle_link_as_quick_reply_button(self):
        text = "Done.\n\n---\n<https://github.com/avibe-bot/avibe/pull/298>"
        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.buttons, [])

    def test_process_reply_preserves_plain_markdown_reference_link_block(self):
        text = "Done.\n\n---\n[Release notes](https://example.com)"
        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.buttons, [])

    def test_process_reply_accepts_plain_markdown_link_button_within_group(self):
        # Regression: a plain ``[label](https://…)`` token used to drop EVERY
        # button in the group (its trailing ``(url)`` broke the end-anchored
        # block match). A link inside a ``|`` group must render as a button, with
        # the label as the payload and the URL discarded.
        reply = process_reply(
            "Done.\n\n---\n"
            "[👀 我先确认] | [🔗 看 PR](https://github.com/avibe-bot/avibe/pull/451) | [✅ 直接合并]"
        )

        self.assertEqual(reply.text, "Done.")
        self.assertEqual(
            [button.text for button in reply.buttons],
            ["👀 我先确认", "🔗 看 PR", "✅ 直接合并"],
        )

    def test_process_reply_accepts_plain_markdown_link_button_as_last_token(self):
        reply = process_reply("Done.\n\n---\n[A] | [docs](https://example.com)")

        self.assertEqual(reply.text, "Done.")
        self.assertEqual([button.text for button in reply.buttons], ["A", "docs"])

    def test_process_reply_preserves_lone_plain_link_with_pipe_in_url(self):
        # A lone reference link whose URL contains ``|`` must still be preserved
        # as text: the lone-link disambiguation matches the whole block rather than
        # scanning for a stray ``|`` (which a URL may legitimately hold).
        text = "Done.\n\n---\n[chart](https://example.com/a?b=1|2)"
        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.buttons, [])

    def test_process_reply_preserves_multiple_plain_reference_links_without_separator(self):
        # Several plain Markdown links after ``---`` with no ``|`` separator are a
        # reference-link section, not a button group — they must stay as text.
        text = "Done.\n\n---\n[Release notes](https://example.com/r)\n[Changelog](https://example.com/c)"
        reply = process_reply(text)

        self.assertEqual(reply.text, text)
        self.assertEqual(reply.buttons, [])

    def test_process_reply_accepts_plain_link_button_with_balanced_parens_in_url(self):
        # A plain-link button whose URL contains balanced parentheses (e.g. a
        # Wikipedia ``A_(B)`` target) must not truncate at the first ``)`` and drop
        # the group.
        reply = process_reply("Done.\n\n---\n[Wiki](https://en.wikipedia.org/wiki/A_(B)) | [Done]")

        self.assertEqual(reply.text, "Done.")
        self.assertEqual([button.text for button in reply.buttons], ["Wiki", "Done"])

    def test_prompt_includes_harness_architecture_and_memory_context(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )
        enabled_agents = [
            SimpleNamespace(
                name="codex",
                normalized_name="codex",
                backend="codex",
                description="Codex compatibility Agent for existing sessions",
            ),
            SimpleNamespace(
                name="Release Auditor",
                normalized_name="release-auditor",
                backend="claude",
                description="Review releases | verify follow-up risk",
            ),
            SimpleNamespace(
                name="--Review Bot",
                normalized_name="",
                backend="codex",
                description="Name needs prompt-safe normalization",
            ),
        ]

        with (
            patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")),
            patch("core.show_git.show_git_checkpointing_active", return_value=True),
            # The tool-policy paragraphs vary with SDK hook support, so pin it
            # rather than let the installed SDK decide what this test asserts.
            # Pinned to True on purpose: a Codex prompt must not inherit the
            # Claude enforcement claim just because that SDK happens to be
            # installed alongside it.
            patch("core.system_prompt_injection._claude_sdk_hooks_available", return_value=True),
        ):
            prompt = build_system_prompt_injection(
                include_quick_replies=True,
                context=context,
                enabled_agents=enabled_agents,
                current_agent_backend="codex",
            )

        self.assertIn("## Show Pages", prompt)
        self.assertIn("`vibe show path`", prompt)
        self.assertIn("`vibe show status`", prompt)
        self.assertIn("`vibe show update --visibility private`", prompt)
        self.assertIn("History is saved automatically around each turn", prompt)
        self.assertIn("`git -C <workspace> status / log / diff / show`", prompt)
        self.assertIn("Restore only via `git restore --source=<ref> -- <path>`", prompt)
        self.assertIn("Never move HEAD, switch branches, rewrite history, or run gc", prompt)
        self.assertIn("Never add remotes, push, or publish the workspace anywhere", prompt)
        self.assertNotIn("`vibe show path --session-id sesk8m4q2p7x`", prompt)
        self.assertIn("Make the page work reasonably on mobile", prompt)
        self.assertIn("managed React/Vite apps", prompt)
        self.assertIn("Tailwind CSS v4 utility classes are built in", prompt)
        self.assertIn("restyle the built-in `@/components/ui/*` components", prompt)
        self.assertIn('must keep `@import "tailwindcss";` and `@import "@avibe/show-ui/theme.css";` at the top', prompt)
        self.assertNotIn("Ready to visualize", prompt)
        self.assertIn("@/components/ui/progress", prompt)
        self.assertNotIn("Excalidraw-style static SVG/PNG diagrams", prompt)
        self.assertNotIn("Avibe Cloud is not connected", prompt)
        self.assertIn("## Harness", prompt)
        self.assertNotIn("## Scheduled tasks, watches, and hooks", prompt)
        self.assertIn("Avibe Harness turns user intent into durable Agent work", prompt)
        self.assertIn("context, owner, trigger, session continuity, delivery target, and observable progress", prompt)
        self.assertIn("Avibe Harness is the first-choice automation layer", prompt)
        self.assertIn("route through `vibe agent`, `vibe task`, and `vibe watch` before backend-native subagents", prompt)
        self.assertIn("native workflow tools, backend-native skills", prompt)
        self.assertIn("Do not default to backend-native automation just because the backend exposes it", prompt)
        self.assertIn("Use backend-native config, skills, subagents, or workflow tools only when the user explicitly asks for backend-native behavior", prompt)
        # This is a Codex session, and only the Claude session handler installs
        # the tool-layer gate, so the prompt must claim no enforcement here even
        # though the Claude SDK is importable (pinned above).
        self.assertIn("Backend-native background work is not gated in this runtime", prompt)
        self.assertNotIn("blocked at the tool layer", prompt)
        self.assertNotIn("only partly blocked", prompt)
        self.assertIn("Route that work through the Harness instead", prompt)
        self.assertIn("Never detach with `nohup` or a trailing `&` for work whose result you need", prompt)
        self.assertIn("what outcome is the user trying to secure", prompt)
        self.assertIn("If the answer is an operating loop, build a Harness instead of only doing the visible step", prompt)
        self.assertIn("### Mental model", prompt)
        self.assertIn("| Agent | Reusable role: backend, model, prompt, description, enabled state | Work needs a stable specialist identity |", prompt)
        self.assertIn("| Session | Continuing context for one Agent work lineage | Work should continue or fork context |", prompt)
        self.assertIn("Relationship: Scope routes work; Agent defines who acts; Session holds continuity", prompt)
        self.assertIn("Current session id: `sesk8m4q2p7x`", prompt)
        self.assertEqual(prompt.count("Current session id: `sesk8m4q2p7x`"), 2)
        self.assertNotIn("Current Agent backend", prompt)
        self.assertNotIn("copying `sesk8m4q2p7x` into the command", prompt)
        self.assertNotIn("generic reply destination", prompt)
        self.assertNotIn("delivery address", prompt)
        self.assertNotIn("Legacy session key:", prompt)
        self.assertNotIn("--session-key", prompt)
        self.assertNotIn("Channel-level session key:", prompt)
        self.assertIn("### Inspecting Harness state", prompt)
        self.assertIn("Use `vibe data query` to inspect Avibe state with guarded read-only SQL", prompt)
        self.assertIn("select name from sqlite_master where type='table' order by name", prompt)
        self.assertIn("schema discovery, current session lookup, existing task/watch inspection, Agent run history", prompt)
        self.assertIn("### Choosing the right Harness shape", prompt)
        self.assertIn("| Independent Agent delegation | `vibe agent run --agent <agent-name>` |", prompt)
        self.assertIn("| Continue a pointed Session | `vibe agent run --session-id ...` |", prompt)
        self.assertIn(
            "| Inspect queued Workbench Session input | `vibe session queue list <session-id>` |",
            prompt,
        )
        self.assertIn(
            "| Remove one queued Workbench Session input | `vibe session queue remove <session-id> <message-id>` |",
            prompt,
        )
        self.assertIn(
            "| Dispatch an existing queued Workbench Session head now | `vibe session send-now <session-id>` |",
            prompt,
        )
        self.assertIn("| Branch from current Session context | `vibe agent run --fork-self ...` |", prompt)
        self.assertIn("Tasks created from an Avibe Agent shell continue this conversation by default", prompt)
        self.assertIn("`vibe task add` creates a time-triggered saved Agent message", prompt)
        self.assertIn("Watches created from an Avibe Agent shell follow up in this conversation by default", prompt)
        self.assertIn("`vibe watch add` creates a managed monitor", prompt)
        self.assertIn("product signals, business events, files, logs, CI/reviews/deploys", prompt)
        delegate_guidance = (
            "Use `vibe agent run --agent <agent-name> --message ...` when one Agent delegates work to another Agent. "
            "By default this creates a background Session in the caller's scope and returns immediately; when the run "
            "completes, the final result is sent back to this conversation. Background Sessions stay out of the session "
            "list and never deliver outward, but remain visible in the Agents run graph, where the user can open their "
            "full chat history or promote them at any time. Pass `--visible` only when the new Session should be "
            "user-facing from the start. Pass `--sync` only when the current process must wait for the result. Pass "
            "`--no-callback` only when you intentionally want no automatic follow-up and will inspect the run later; "
            "pass `--callback-session-id <id>` only to route the final result elsewhere. Add `--scope-id <scopes.id>` "
            "only when placing the new Session in a specific existing scope."
        )
        self.assertIn(delegate_guidance, prompt)
        self.assertNotIn("Outside an Agent shell, a caller-less run", prompt)
        self.assertIn("Pass `--sync` only when the current process must wait for the result", prompt)
        self.assertIn(
            "That existing-Session send queues behind an active turn by default",
            prompt,
        )
        self.assertIn(
            "an explicit user request is one signal, not a prerequisite",
            prompt,
        )
        self.assertIn(
            "Both forms require a Web/Workbench Session",
            prompt,
        )
        self.assertIn(
            "vibe agent run --session-id <id> --send-now --message ...",
            prompt,
        )
        self.assertIn(
            "vibe session send-now <id>",
            prompt,
        )
        self.assertIn(
            "vibe session queue list <id>",
            prompt,
        )
        self.assertIn(
            "vibe session queue remove <id> <message-id>",
            prompt,
        )
        self.assertIn(
            "Always list first and use the returned stable message id",
            prompt,
        )
        self.assertIn(
            "the new message does not leapfrog it",
            prompt,
        )
        self.assertIn(
            "If interruption is refused, the active turn and durable queue remain intact",
            prompt,
        )
        self.assertNotIn("Add `--same-scope` to require the caller/source scope", prompt)
        self.assertIn("Use `vibe agent run --fork-self --message ...` when work should branch from this current Session", prompt)
        self.assertIn("Forks keep the source Session backend, scope, and cwd by default", prompt)
        self.assertIn("It does not change that Session's cwd, scope, Agent, model, or reasoning settings", prompt)
        self.assertNotIn("`--prefix` is legacy-compatible", prompt)
        self.assertNotIn("`--post-to` is a delivery override", prompt)
        self.assertIn("Prefer `--same-scope` or `--scope-id <scopes.id>` for new Session placement", prompt)
        self.assertNotIn("--deliver-key", prompt)
        self.assertIn("Manage existing work with `vibe task <list|show|pause|resume|run|remove>`", prompt)
        self.assertIn("`vibe watch <list|show|pause|resume|remove>`", prompt)
        self.assertIn("`vibe runs <list|show|cancel>`", prompt)
        self.assertIn("The CLI exposes more options than this prompt lists", prompt)
        self.assertIn("`vibe <command> <subcommand> --help`", prompt)
        self.assertIn("### Agents", prompt)
        self.assertIn("| Agent Name | Backend | Agent Description |", prompt)
        self.assertIn("| codex | codex | Codex compatibility Agent for existing sessions |", prompt)
        self.assertIn(r"| release-auditor | claude | Review releases \| verify follow-up risk |", prompt)
        self.assertIn("| review-bot | codex | Name needs prompt-safe normalization |", prompt)
        self.assertIn("generated from currently enabled Agents at prompt-injection time", prompt)
        self.assertIn("The `Agent Name` column is command-safe", prompt)
        self.assertNotIn("CLI Token", prompt)
        self.assertIn("Use the `Agent Name` value exactly as listed in shell commands", prompt)
        self.assertIn("`--session-id <id>` resumes that exact Agent Session and its transcript, backend identity, Show Page, and routing", prompt)
        self.assertIn("Without `--session-id`, `--fork-self`, or `--fork-session`, `vibe agent run --agent <agent-name>` creates a separate background Session", prompt)
        self.assertIn(
            "Use `vibe session update --visible|--hidden` (`--visibility foreground|background`)",
            prompt,
        )
        self.assertIn("`--fork-self` creates a new Agent Session from this current Session's native backend context", prompt)
        self.assertIn("`--fork-session <id>` creates a new Agent Session from that explicit source Session's native backend context", prompt)
        self.assertIn("vibe agent run --agent <agent-name> --message ...", prompt)
        self.assertIn("vibe agent run --agent <agent-name> --session-id ... --message ...", prompt)
        self.assertIn("Async callbacks return to this conversation by default", prompt)
        self.assertNotIn("Reuse an existing Session only with Agents whose `Backend` matches", prompt)
        self.assertIn("With `--fork-self` or `--fork-session`, pass `--agent`, `--model`, or `--reasoning-effort` only as forked-Session overrides", prompt)
        self.assertIn("`--sync` changes waiting behavior, not session identity", prompt)
        self.assertIn("synchronous runs wait for the result and are still recorded in `vibe runs`", prompt)
        self.assertNotIn("--create-session-per-run", prompt)
        self.assertIn("Create or update Agents only when it captures a reusable role", prompt)
        self.assertIn("## Memory and Project Context", prompt)
        self.assertIn("A shared user context and preferences file is available at ", prompt)
        self.assertIn("/tmp/user_preferences.md", prompt)
        self.assertIn("Use the right memory surface", prompt)
        self.assertIn("project lessons, conventions, architecture, workflows, and pointers go to the nearest relevant `AGENTS.md`", prompt)
        self.assertIn("`AGENTS.md` is an index, not a log", prompt)
        self.assertIn("update by consolidating and abstracting instead of merely appending", prompt)
        self.assertIn("Use the current platform `slack`", prompt)
        self.assertIn("`slack/<user_id>`", prompt)
        self.assertNotIn("slack/U1", prompt)
        self.assertIn("Only record durable, factual, reusable information there.", prompt)
        self.assertIn("Keep entries short, deduplicated, and free of secrets unless the user explicitly asks.", prompt)
        self.assertIn("use `vibe data query` to recover Sessions and Messages by keyword, time, scope, Agent, or run history", prompt)

    def test_show_pages_prompt_reports_history_unavailable_without_git(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with (
            patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")),
            patch("core.show_git.show_git_checkpointing_active", return_value=False),
        ):
            prompt = build_system_prompt_injection(include_quick_replies=True, context=context)

        self.assertIn("Automatic Show Page history is unavailable", prompt)
        self.assertNotIn("History is saved automatically around each turn", prompt)
        self.assertNotIn("git restore --source", prompt)

    def test_show_pages_prompt_rechecks_mid_session_ownership_flip(self):
        session_id = "sesk8m4q2p7x"
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": session_id},
        )
        workspace = paths.get_show_page_dir(session_id)
        workspace.mkdir(parents=True)

        with (
            patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")),
            patch("core.show_git.show_git_checkpointing_active", return_value=True),
        ):
            managed_prompt = build_system_prompt_injection(include_quick_replies=True, context=context)
            (workspace / ".git").mkdir()
            self_managed_prompt = build_system_prompt_injection(include_quick_replies=True, context=context)
            (workspace / ".git").rmdir()
            managed_again_prompt = build_system_prompt_injection(include_quick_replies=True, context=context)

        self.assertIn("History is saved automatically around each turn", managed_prompt)
        self.assertNotIn("shadow history continues automatically", managed_prompt)
        self.assertIn("Avibe's shadow history continues automatically", self_managed_prompt)
        self.assertIn("addresses the **user's repo**, not Avibe history", self_managed_prompt)
        self.assertIn("Only if the user explicitly asks to recover from Avibe history", self_managed_prompt)
        self.assertNotIn("Read freely: `git -C <workspace>", self_managed_prompt)
        self.assertNotIn("Restore only via `git restore", self_managed_prompt)
        self.assertIn("History is saved automatically around each turn", managed_again_prompt)
        self.assertNotIn("shadow history continues automatically", managed_again_prompt)

    def test_prompt_does_not_render_empty_agents_as_invokable_table_row(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            missing_store_prompt = build_system_prompt_injection(
                include_quick_replies=False,
                context=context,
                enabled_agents=None,
            )
            empty_store_prompt = build_system_prompt_injection(
                include_quick_replies=False,
                context=context,
                enabled_agents=[],
            )

        self.assertIn("No enabled Agents were provided in this prompt context.", missing_store_prompt)
        self.assertIn("run `vibe agent list`", missing_store_prompt)
        self.assertIn("No Agents are currently enabled.", empty_store_prompt)
        self.assertIn("Do not run `vibe agent show` or `vibe agent run`", empty_store_prompt)
        self.assertNotIn("| (none) |", missing_store_prompt)
        self.assertNotIn("| (none) |", empty_store_prompt)

    def test_show_pages_prompt_mentions_avibe_cloud_when_not_connected(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            prompt = build_system_prompt_injection(
                include_quick_replies=False,
                avibe_cloud_connected=False,
                context=context,
            )

        self.assertIn("## Show Pages", prompt)
        self.assertIn("⚠️ Avibe Cloud is not connected", prompt)
        self.assertIn("register an avibe.bot account", prompt)
        self.assertIn("`vibe remote pair`", prompt)

    def test_show_pages_prompt_allows_literal_typescript_braces(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            prompt = build_system_prompt_injection(
                include_quick_replies=False,
                context=context,
            )

        self.assertIn("`vibe show path`", prompt)
        self.assertNotIn("`vibe show path --session-id sesk8m4q2p7x`", prompt)
        self.assertIn("export async function GET(request) { return Response.json({ ok: true }) }", prompt)

    def test_show_pages_prompt_includes_annotation_capability_guidance(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_session_id": "sesk8m4q2p7x"},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            prompt = build_system_prompt_injection(
                include_quick_replies=False,
                context=context,
            )

        guidance = """### Show Page annotations & reverse marks
- Users can annotate your Show Page; each annotation arrives as a chat message tagged [show-annotation] with its event id. Some messages end with a ready-to-run reply command — whether to reply on the page or respond by editing the page content is your call, per scenario.
- After reworking a page area you may leave a short callout: `vibe show mark <selector-or-anchor> --message '...'` (same target replaces), or an `agent-note="..."` attribute on elements you author. Marks retire once read — leave at most 1-2 per turn.
- Inspect/withdraw: `vibe show marks` / `vibe show unmark <id|target> ...`; toggle the user's annotation mode: `vibe show annotate --on|--off [--mode smart|screenshot]`."""
        self.assertIn(guidance, prompt)
        self.assertNotIn("prefer replying on the page", prompt)

    def test_prompt_uses_fallback_platform_for_unannotated_context(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            thread_id="171717.123",
            platform_specific={"is_dm": False},
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            with self.assertRaisesRegex(ValueError, "agent_session_id is required"):
                build_system_prompt_injection(
                    include_quick_replies=True,
                    context=context,
                    fallback_platform="slack",
                )

    def test_prompt_handles_missing_platform_specific(self):
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform=None,
            platform_specific=None,
        )

        with patch.object(paths, "get_user_preferences_path", return_value=Path("/tmp/user_preferences.md")):
            with self.assertRaisesRegex(ValueError, "agent_session_id is required"):
                build_system_prompt_injection(
                    include_quick_replies=True,
                    context=context,
                    fallback_platform="slack",
                )

    def test_file_links_with_parentheses_are_preserved(self):
        enhanced = process_reply("![video](file:///Users/test/SaveTwitter.Net_GABV3XNWYAARAZz(gif).mp4)")

        self.assertEqual(len(enhanced.files), 1)
        self.assertEqual(
            enhanced.files[0].path,
            "/Users/test/SaveTwitter.Net_GABV3XNWYAARAZz(gif).mp4",
        )

    def test_windows_file_uri_is_normalized_before_absolute_check(self):
        with patch("core.reply_enhancer.os.name", "nt"), patch("core.reply_enhancer.os.path.isabs") as isabs:
            isabs.side_effect = lambda value: value == r"C:\Users\test\generated image.png"
            enhanced = process_reply("![generated image](file:///C:/Users/test/generated%20image.png)")

        self.assertEqual(len(enhanced.files), 1)
        self.assertEqual(enhanced.files[0].path, r"C:\Users\test\generated image.png")

    async def test_wechat_result_ignores_quick_reply_buttons(self):
        controller = _StubController("wechat")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        await dispatcher.emit_agent_message(
            context,
            "result",
            "Done.\n---\n[继续] | [提交PR]",
        )

        self.assertEqual(controller.im_client.sent_button_messages, [])
        self.assertEqual(
            controller.im_client.sent_messages,
            [("C1", "Done.", "markdown")],
        )

    async def test_lark_quick_reply_buttons_use_horizontal_layout(self):
        controller = _StubController("lark")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="lark")

        await dispatcher.emit_agent_message(
            context,
            "result",
            "Done.\n---\n[继续] | [提交PR]",
        )

        self.assertEqual(len(controller.im_client.sent_button_messages), 1)
        keyboard = controller.im_client.sent_button_messages[0][3]
        # Lark quick replies are now multi-column (cap 3/row), so two buttons
        # share a single row instead of stacking vertically.
        self.assertEqual([[button.text for button in row] for row in keyboard.buttons], [["继续", "提交PR"]])

    async def test_markdown_link_style_quick_reply_dispatches_label_callbacks(self):
        controller = _StubController("slack")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")

        await dispatcher.emit_agent_message(
            context,
            "result",
            "Done.\n---\n"
            "[:eyes: 看 PR](<https://github.com/avibe-bot/avibe/pull/298>) | "
            "[:rocket: 等评审完合并]",
        )

        self.assertEqual(len(controller.im_client.sent_button_messages), 1)
        keyboard = controller.im_client.sent_button_messages[0][3]
        buttons = keyboard.buttons[0]
        self.assertEqual([button.text for button in buttons], [":eyes: 看 PR", ":rocket: 等评审完合并"])
        self.assertEqual(
            [button.callback_data for button in buttons],
            ["quick_reply::eyes: 看 PR", "quick_reply::rocket: 等评审完合并"],
        )

    async def test_lark_log_message_strips_file_links_before_sending(self):
        controller = _StubController("lark")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="lark")

        await dispatcher.emit_agent_message(
            context,
            "assistant",
            "Preview ready\n\n![screen](file:///tmp/screen-room.png)",
        )

        self.assertEqual(
            controller.im_client.sent_messages,
            [("C1", "Preview ready\n\nscreen", "markdown")],
        )

    async def test_lark_log_message_preserves_button_like_markdown_blocks(self):
        controller = _StubController("lark")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="lark")

        await dispatcher.emit_agent_message(
            context,
            "assistant",
            "Runbook\n---\n[step one] | [step two]",
        )

        self.assertEqual(
            controller.im_client.sent_messages,
            [("C1", "Runbook\n---\n[step one] | [step two]", "markdown")],
        )

    async def test_telegram_quick_reply_buttons_use_vertical_layout(self):
        controller = _StubController("telegram")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="telegram")

        await dispatcher.emit_agent_message(
            context,
            "result",
            "Done.\n---\n[继续] | [提交PR]",
        )

        self.assertEqual(len(controller.im_client.sent_button_messages), 1)
        keyboard = controller.im_client.sent_button_messages[0][3]
        self.assertEqual([[button.text for button in row] for row in keyboard.buttons], [["继续"], ["提交PR"]])

    async def test_discord_long_result_splits_into_multiple_messages_without_markdown_attachment(self):
        controller = _StubController("discord")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="discord")
        long_text = " ".join(["Alpha"] * 320) + "\n\n" + " ".join(["Beta"] * 120)

        message_id = await dispatcher.emit_agent_message(context, "result", long_text)

        self.assertEqual(message_id, "msg-1")
        self.assertGreater(len(controller.im_client.sent_messages), 1)
        self.assertEqual(
            "".join(text for _, text, _ in controller.im_client.sent_messages),
            long_text,
        )
        self.assertEqual(controller.im_client.uploaded_markdowns, [])


if __name__ == "__main__":
    unittest.main()
