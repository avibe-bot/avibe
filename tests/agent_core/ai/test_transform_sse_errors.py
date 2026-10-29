from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from core.agent_core.ai._common import endpoint_origin
from core.agent_core.ai.errors import classify_error, redact_provider_error, sanitize_endpoint_text
from core.agent_core.ai.provider import ModelEndpoint
from core.agent_core.ai.sse import SSEParseError, SSEParser
from core.agent_core.ai.transform import SYNTHETIC_TOOL_RESULT, transform_messages
from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

_PROTOCOL_ORIGINS = (
    Origin("anthropic", "anthropic", "model"),
    Origin("openai", "openai_chat", "model"),
    Origin("openai", "openai_responses", "model"),
)


def _redacted_error(**kwargs: object):
    return redact_provider_error(classify_error(**kwargs))


def test_sse_parser_handles_split_crlf_comments_and_multiline_data() -> None:
    parser = SSEParser()
    events = []
    for chunk in (b": keepalive\r", b"\n", b"event: message\r\n", b"data: one\r\n", b"data: two\r", b"\n\r\n"):
        events.extend(parser.feed(chunk))

    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].data == "one\ntwo"


def test_sse_parser_preserves_utf8_codepoints_split_across_byte_chunks() -> None:
    parser = SSEParser()
    events = []
    encoded = "data: 你好\n\n".encode("utf-8")
    for byte in encoded:
        events.extend(parser.feed(bytes([byte])))

    assert events[0].data == "你好"


def test_sse_parser_flushes_a_final_cr_line_ending() -> None:
    assert SSEParser().feed("data: final\r") == []
    parser = SSEParser()
    parser.feed("data: final\r")
    assert parser.finish()[0].data == "final"


def test_sse_parser_strips_one_utf8_bom_at_stream_start() -> None:
    parser = SSEParser()
    events = parser.feed(b"\xef\xbb\xbfdata: first\n\n")
    events.extend(parser.feed("data: second\n\n"))

    assert [event.data for event in events] == ["first", "second"]


def test_sse_parser_caps_pending_line_and_event_data() -> None:
    parser = SSEParser(max_line_size=8, max_event_size=12)

    with pytest.raises(SSEParseError, match="line"):
        parser.feed("data: 123")

    parser = SSEParser(max_line_size=128, max_event_size=8)
    parser.feed("data: 1234\n")
    with pytest.raises(SSEParseError, match="event"):
        parser.feed("data: 5678\n")

    large_event = SSEParser().feed(f"data: {'x' * (70 * 1024)}\n\n")
    assert large_event[0].data == "x" * (70 * 1024)

    with pytest.raises(ValueError, match="positive"):
        SSEParser(max_line_size=0)


def test_sse_parser_scans_many_short_lines_without_changing_event_shape() -> None:
    parser = SSEParser()
    line_count = 10_000

    events = parser.feed(("data: x\n" * line_count) + "\n")

    assert len(events) == 1
    assert events[0].data == "\n".join(["x"] * line_count)


def test_cross_provider_transform_drops_opaque_payload_and_answers_orphaned_calls() -> None:
    source = Origin("openai", "openai_chat", "foreign-test")
    target = Origin("anthropic", "anthropic", "claude-test")
    call = ToolCallBlock("call|foreign", "read", {"path": "x"}, signature="foreign-opaque")
    messages = (
        UserMessage((ImageBlock("image/png", "token", "shot.png"),)),
        AssistantMessage(
            content=(ThinkingBlock("private", "opaque"), call),
            origin=source,
            stop_reason="tool_use",
        ),
    )

    result = transform_messages(messages, target, supports_images=False)

    assistant = result.messages[1]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.content == (
        ToolCallBlock(
            id=result.tool_call_id_map["call|foreign"],
            name="read",
            arguments={"path": "x"},
        ),
    )
    assert result.messages[0] == UserMessage((TextBlock(text="[image: shot.png]"),))
    synthetic = result.messages[-1]
    assert synthetic.tool_call_id == result.tool_call_id_map["call|foreign"]  # type: ignore[attr-defined]
    assert synthetic.content == (TextBlock(text=SYNTHETIC_TOOL_RESULT),)  # type: ignore[attr-defined]
    assert synthetic.is_error is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("served_by", "hop", "bound"),
    [
        ("sha256:account-one", "sha256:account-one", True),
        ("sha256:account-one", "sha256:account-two", False),
        (None, "sha256:account-two", False),
        (None, None, True),
    ],
    ids=["same-identity", "another-identity", "unknown-identity", "no-identity-anywhere"],
)
def test_opaque_payloads_go_back_only_to_the_source_identity_that_served_them(served_by, hop, bound) -> None:
    """cross-provider.md: another account refuses a Source's ciphertext (xAI's and OpenAI's encrypted reasoning), so a
    message's signatures, redacted thinking and native ids go back only to a hop of the Source identity that served
    it: another Source, or the same Source after its key changed, is another identity. A message no known identity
    served, as one written before ``source_identity``, goes to a hop that names one as a different origin's."""

    origin = Origin("openai", "openai_responses", "gpt-test")
    reasoning = ThinkingBlock("plan", '{"id": "rs_1", "encrypted_content": "account-bound"}')
    redacted = ThinkingBlock("", "redacted-opaque", redacted=True)
    call = ToolCallBlock("call_1", "read", {"path": "x"}, native_id="fc_1", signature="call-opaque")
    message = AssistantMessage((reasoning, redacted, call), origin, "tool_use", source_identity=served_by)
    endpoint = ModelEndpoint(
        "openai_responses", "http://hub.invalid/v1", "gpt-test", "token", source_id="src_account0001",
        source_identity=hop,
    )

    result = transform_messages((UserMessage((TextBlock("go"),)), message), origin, endpoint=endpoint)

    sent = result.messages[1]
    assert isinstance(sent, AssistantMessage)
    if bound:
        assert sent.content == (reasoning, redacted, call)
    else:
        assert sent.content == (ToolCallBlock("call_1", "read", {"path": "x"}),)


_THINKER = Origin("openai", "openai_responses", "gpt-test")


@pytest.mark.parametrize(
    "target",
    [
        Origin("anthropic", "anthropic", "claude-test"),
        Origin("openai", "openai_chat", "gpt-test"),
        Origin("openai", "openai_responses", "gpt-other"),
        _THINKER,
    ],
    ids=["to-anthropic", "to-chat", "to-another-model", "same-origin"],
)
def test_thinking_goes_back_only_to_the_origin_that_thought_it(target: Origin) -> None:
    """cross-provider.md: thinking is a model's private reasoning, not something the assistant said. Another origin
    gets none of it, signed, unsigned or redacted, and never as assistant text (Anthropic refuses such requests as
    reasoning extraction); everything the assistant said or did still goes. A response whose only content was that
    thinking is left out, not sent as an empty assistant message, while one that was empty as served (a refusal) still
    goes. Its own origin gets it back as before."""

    signed = ThinkingBlock("signed plan", '{"id": "rs_1", "encrypted_content": "enc"}')
    redacted = ThinkingBlock("", "redacted-opaque", redacted=True)
    call = ToolCallBlock("call_1", "read", {"path": "x"})
    result = ToolResultMessage("call_1", "read", (TextBlock("contents"),))
    history = (
        UserMessage((TextBlock("go"),)),
        AssistantMessage((signed, ThinkingBlock("unsigned plan"), redacted, TextBlock("reading"), call), _THINKER, "tool_use"),
        result,
        AssistantMessage((ThinkingBlock("only thought", "sig-only"), ThinkingBlock("more thought")), _THINKER, "stop"),
        UserMessage((TextBlock("and then?"),)),
        AssistantMessage((), _THINKER, "refusal"),
        UserMessage((TextBlock("please"),)),
    )

    sent = transform_messages(history, target).messages

    if target == _THINKER:
        assert sent == (
            history[0],
            AssistantMessage(
                (signed, TextBlock("unsigned plan"), redacted, TextBlock("reading"), call), _THINKER, "tool_use"
            ),
            result,
            AssistantMessage((ThinkingBlock("only thought", "sig-only"), TextBlock("more thought")), _THINKER, "stop"),
            *history[4:],
        )
        return
    assert sent == (
        history[0],
        AssistantMessage((TextBlock("reading"), call), _THINKER, "tool_use"),
        result,
        *history[4:],
    )


def test_cross_provider_transform_drops_results_for_failed_tool_turns() -> None:
    history = (
        UserMessage((TextBlock(text="request"),)),
        AssistantMessage(
            content=(ToolCallBlock("failed-call", "read", {}),),
            origin=Origin("openai", "openai_chat", "model"),
            stop_reason="error",
        ),
        ToolResultMessage(
            tool_call_id="failed-call",
            tool_name="read",
            content=(TextBlock(text="settled"),),
            is_error=True,
        ),
        UserMessage((TextBlock(text="next request"),)),
    )

    transformed = transform_messages(history, Origin("anthropic", "anthropic", "model"))

    assert not any(
        isinstance(message, ToolResultMessage) and message.tool_call_id == "failed-call"
        for message in transformed.messages
    )


def test_transform_keeps_normalized_tool_ids_unique_per_request() -> None:
    unsafe_id = "bad:id"
    colliding_safe_id = "call_" + hashlib.sha256(unsafe_id.encode()).hexdigest()[:24]
    history = (
        AssistantMessage(
            content=(
                ToolCallBlock(unsafe_id, "first", {"n": 1}),
                ToolCallBlock(colliding_safe_id, "second", {"n": 2}),
            ),
            origin=Origin("openai", "openai_responses", "model"),
            stop_reason="tool_use",
        ),
    )

    transformed = transform_messages(history, Origin("anthropic", "anthropic", "model"))
    assistant = transformed.messages[0]
    assert isinstance(assistant, AssistantMessage)
    assert len({call.id for call in assistant.tool_calls}) == 2
    assert assistant.tool_calls[0].id == transformed.tool_call_id_map[unsafe_id]
    assert assistant.tool_calls[1].id == transformed.tool_call_id_map[colliding_safe_id]


def test_transform_correlates_reused_tool_ids_by_turn() -> None:
    origin = Origin("openai", "openai_chat", "model")
    history = (
        AssistantMessage(
            content=(ToolCallBlock("reused", "read", {"turn": 1}),),
            origin=origin,
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            tool_call_id="reused",
            tool_name="read",
            content=(TextBlock(text="first"),),
        ),
        AssistantMessage(
            content=(ToolCallBlock("reused", "read", {"turn": 2}),),
            origin=origin,
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            tool_call_id="reused",
            tool_name="read",
            content=(TextBlock(text="second"),),
        ),
    )

    transformed = transform_messages(history, Origin("anthropic", "anthropic", "model"))
    assistants = [message for message in transformed.messages if isinstance(message, AssistantMessage)]
    results = [message for message in transformed.messages if isinstance(message, ToolResultMessage)]

    assert len(assistants) == 2
    assert len(results) == 2
    assert assistants[0].tool_calls[0].id != assistants[1].tool_calls[0].id
    assert results[0].tool_call_id == assistants[0].tool_calls[0].id
    assert results[1].tool_call_id == assistants[1].tool_calls[0].id


def test_empty_provider_custom_endpoints_have_distinct_origins() -> None:
    left = endpoint_origin(
        ModelEndpoint("openai_chat", "https://one.example/v1", "same-model", "token")
    )
    right = endpoint_origin(
        ModelEndpoint("openai_chat", "https://two.example/v1", "same-model", "token")
    )

    assert left != right
    assert left.provider.startswith("endpoint:")
    assert right.provider.startswith("endpoint:")


def test_unnamed_custom_endpoint_origin_is_credential_free() -> None:
    origin = endpoint_origin(
        ModelEndpoint(
            "openai_chat",
            "https://user:secret@one.example:8443/v1?api_key=query-secret",
            "same-model",
            "token",
        )
    )

    assert "secret" not in origin.provider
    assert "query-secret" not in origin.provider
    assert origin.provider == "endpoint:https://one.example:8443/v1"


def test_endpoint_redaction_handles_punctuation_in_credentials() -> None:
    endpoint = "https://user:p,ss@one.example:8443/v1?token=query-secret"

    message = sanitize_endpoint_text(f"request failed at {endpoint}", endpoint)

    assert message == "request failed at https://one.example:8443/v1"


def test_error_classification_preserves_retry_after_and_stream_boundary() -> None:
    limited = classify_error(status=429, body='{"error":{"message":"slow down"}}', headers={"Retry-After": "2"})
    assert limited.kind == "rate_limit"
    assert limited.retryable is True
    assert limited.retry_after_s == 2

    overflow = classify_error(status=400, body="context_length_exceeded")
    assert overflow.kind == "overflow"
    assert overflow.retryable is False

    streamed = classify_error(status=503, body="overloaded", streamed=True)
    assert streamed.kind == "overloaded"
    assert streamed.retryable is False


def test_overflow_classifies_cerebras_bodyless_status() -> None:
    assert classify_error(status=400, body="400 status code (no body)").kind == "overflow"


@pytest.mark.parametrize("status", [400, 429])
@pytest.mark.parametrize("representation", ["plain", "json", "json-string"])
@pytest.mark.parametrize(
    ("message", "kind", "retryable"),
    [
        ("Throttling error: Too many tokens, please wait before trying again.", "invalid_request", False),
        ("Service unavailable: Too many tokens, please wait before trying again.", "server", True),
        (" \n\ttHrOtTlInG eRrOr: too many tokens\r\n", "invalid_request", False),
        (" \n\tsErViCe UnAvAiLaBlE: too many tokens\r\n", "server", True),
        ("ThrottlingException: too many tokens", "invalid_request", False),
        ("com.amazonaws.bedrock.ThrottlingException: too many tokens", "invalid_request", False),
    ],
)
def test_non_overflow_prefixes_survive_message_extraction(
    status: int, representation: str, message: str, kind: str, retryable: bool,
) -> None:
    body = {
        "plain": message,
        "json": json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}}),
        "json-string": json.dumps(message),
    }[representation]
    if status == 429:
        kind, retryable = "rate_limit", True

    error = classify_error(status=status, body=body)

    assert error.kind == kind
    assert error.retryable is retryable
    assert error.continuable is retryable
    assert error.message == (message.strip() if representation == "plain" else message)


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
@pytest.mark.parametrize(
    "message",
    [
        "too many tokens",
        "'Throttling error: too many tokens'",
        "Request failed: Service unavailable: too many tokens",
        "too many tokens\nThrottling error: previous attempt",
        "too many tokens\nService unavailable: previous attempt",
        "Throttling errorish: too many tokens",
        "Service unavailableish: too many tokens",
        "Throttling error without a colon: too many tokens",
        "Service unavailable without a colon: too many tokens",
    ],
)
def test_non_overflow_prefix_lookalikes_do_not_hide_true_overflow(as_json: bool, message: str) -> None:
    body = json.dumps({"error": {"message": message}}) if as_json else message
    assert classify_error(status=400, body=body).kind == "overflow"


@pytest.mark.parametrize("prefix", ["Throttling error", "Service unavailable"])
def test_non_message_json_fields_cannot_supply_a_non_overflow_prefix(prefix: str) -> None:
    body = json.dumps({"diagnostic": f"{prefix}: previous attempt", "error": {"message": "too many tokens"}}, indent=2)
    assert classify_error(status=400, body=body).kind == "overflow"


@pytest.mark.parametrize(
    ("status", "code", "message", "kind"),
    [
        (400, "ThrottlingException", "too many tokens", "invalid_request"),
        (400, "context_length_exceeded", "Throttling error: too many tokens", "overflow"),
        (400, "request_too_large", "Service unavailable: too many tokens", "overflow"),
        (400, "rate_limit_error", "Throttling error: too many tokens", "rate_limit"),
        (400, "authentication_error", "Service unavailable: too many tokens", "auth"),
        (413, "rate_limit_error", "Throttling error: too many tokens", "overflow"),
        (401, "context_length_exceeded", "Throttling error: too many tokens", "auth"),
        (503, "context_length_exceeded", "Throttling error: too many tokens", "server"),
        (429, "context_length_exceeded", "Throttling error: too many tokens", "rate_limit"),
        (429, "invalid_request_error", "Service unavailable: too many tokens", "rate_limit"),
        (429, "ThrottlingException", "ThrottlingException: too many tokens", "rate_limit"),
    ],
)
def test_non_overflow_prefixes_preserve_status_and_machine_code_priority(
    status: int, code: str, message: str, kind: str,
) -> None:
    body = json.dumps({"error": {"type": code, "message": message}})
    assert classify_error(status=status, body=body).kind == kind


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses_are_not_retryable(status: int) -> None:
    error = classify_error(status=status, body="denied")
    assert error.kind == "auth"
    assert error.retryable is False


@pytest.mark.parametrize(
    "body",
    [
        "provider rejected password=super-secret",
        "provider rejected https://model.test/v1?password=super-secret",
    ],
)
def test_error_message_redacts_password_named_and_query_values(body: str) -> None:
    error = _redacted_error(body=body)

    assert "super-secret" not in error.message
    assert "[redacted]" in error.message or error.message == "provider rejected https://model.test/v1"


@pytest.mark.parametrize(
    "code",
    [
        "authentication_error",
        "invalid_api_key",
        "permission_error",
        "permission",
        "permission_denied",
        "unauthenticated",
    ],
)
def test_provider_auth_codes_are_not_retryable(code: str) -> None:
    error = classify_error(code=code, body=f'{{"error":{{"code":"{code}"}}}}')

    assert error.kind == "auth"
    assert error.retryable is False


def test_specific_provider_code_precedes_nested_detail_code() -> None:
    error = classify_error(
        status=400,
        body=(
            '{"error":{"code":"context_length_exceeded",'
            '"details":[{"type":"rate_limit"}]}}'
        ),
    )

    assert error.kind == "overflow"
    assert error.retryable is False


def test_network_errors_are_retryable_before_any_streamed_output() -> None:
    error = classify_error(exc=httpx.ConnectError("connection failed"))
    assert error.kind == "network"
    assert error.retryable is True


def test_error_json_without_message_redacts_sensitive_values() -> None:
    error = _redacted_error(body='{"token":"secret-token","api_key":"sk-secret"}')

    assert "secret-token" not in error.message
    assert "sk-secret" not in error.message
    assert "[redacted]" in error.message


def test_error_message_redacts_quoted_json_nested_in_message() -> None:
    error = _redacted_error(body='{"message":"{\\"token\\":\\"secret-token\\"}"}')

    assert "secret-token" not in error.message
    assert "[redacted]" in error.message


def test_error_message_redacts_basic_authorization_credentials() -> None:
    error = _redacted_error(body="provider echoed Authorization: Basic dXNlcjpwYXNz")

    assert "dXNlcjpwYXNz" not in error.message
    assert "Basic [redacted]" in error.message


@pytest.mark.parametrize(
    ("body", "secret"),
    [
        ("Authorization: Bearer bearer-secret", "bearer-secret"),
        ("Authorization: Basic basic-secret", "basic-secret"),
        ("Authorization: Token token-secret", "token-secret"),
        ("Authorization: Custom custom-secret", "custom-secret"),
        ("Authorization: Custom realm=x, sig=sig-secret", "sig-secret"),
        ("Authorization: abcdef0123456789secret", "abcdef0123456789secret"),
        ("Authorization: abcdefghijk12345 (expired)", "abcdefghijk12345"),
        ("OPENAI_API_KEY=abcdefghijk12345", "abcdefghijk12345"),
        ("x-litellm-api-key: abcdefghijk12345", "abcdefghijk12345"),
        ("Invalid X-Portkey-Api-Key: pk_abcdef123456", "pk_abcdef123456"),
        ("X-Authorization-Token: plainsecret1", "plainsecret1"),
        ("authorization_token=plainsecret1", "plainsecret1"),
        ("cookie_secret=plainsecret1", "plainsecret1"),
        ("access_key=plainsecret1", "plainsecret1"),
        ("client_key=plainsecret1", "plainsecret1"),
        ('"access_key": "plainsecret1"', "plainsecret1"),
        ('"Authorization":"rawkey-secret"', "rawkey-secret"),
        ("X-Authorization: plain-secret", "plain-secret"),
        ("Helicone-Auth: Bearer helicone-secret", "helicone-secret"),
        ("Access Key: AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("X-Auth-Token: Bearer abc123SECRET", "abc123SECRET"),
        ("X-Api-Key: Bearer abc123SECRET", "abc123SECRET"),
        ("client_secret: two words SECRET", "two words SECRET"),
        ('Authorization: Digest username="u", response="digest-secret"', "digest-secret"),
        ('password=hunter2SECRET is wrong', "hunter2SECRET"),
        ("provider failed: password=secret", "secret"),
        ("api_key='QUOTEDSECRET'", "QUOTEDSECRET"),
        ('password="hunter2SECRET"', "hunter2SECRET"),
        ('token = "quoted-token"', "quoted-token"),
        ("X-API-Key: abc123SECRET\r\nContent-Type: application/json", "abc123SECRET"),
        ("Proxy-Authorization: Custom proxy-secret", "proxy-secret"),
        ("X-Goog-Api-Key: AIza-header-secret", "AIza-header-secret"),
        ("Ocp-Apim-Subscription-Key: subscription-secret", "subscription-secret"),
        ("Invalid API Key: gsk_live-secret", "gsk_live-secret"),
        ('{"detail":{"secretKey":"SKSECRET1"}}', "SKSECRET1"),
        ('{"detail":{"aws_secret_access_key":"AWSSECRET"}}', "AWSSECRET"),
        ('{"detail":{"upstream_access_token_v2":"TOKSECRET"}}', "TOKSECRET"),
        ('{"message":"access_token=access-secret"}', "access-secret"),
        ('{"message":"refresh_token: refresh-secret"}', "refresh-secret"),
        ('{"message":"client_secret=client-secret"}', "client-secret"),
        ('{"X-API-Key":"header-secret"}', "header-secret"),
        ("Cookie: theme=dark; JSESSIONID=cookie-secret", "cookie-secret"),
        ("Set-Cookie: session=set-cookie-secret; Path=/", "set-cookie-secret"),
        ("provider returned sk-live-token", "sk-live-token"),
        ("provider returned AIza-live-token", "AIza-live-token"),
        ("provider returned ghp_live-token", "ghp_live-token"),
        ("upstream rejected Bearer abc123XYZsecretval", "abc123XYZsecretval"),
        (
            "provider returned "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
        ),
    ],
)
def test_error_redactor_covers_key_scheme_and_token_shapes(body: str, secret: str) -> None:
    error = _redacted_error(body=body)

    assert secret not in error.message
    assert "[redacted]" in error.message


@pytest.mark.parametrize(
    "body",
    [
        "max_tokens: 200000 > 64000",
        "thinking.budget_tokens: Input should be positive",
        "model token limit exceeded",
        "Basic validation failed",
        "token_limit: 8192",
        "token_count: 8192",
        '{"token_limit":8192,"token_count":8192}',
    ],
)
def test_error_redactor_does_not_garble_non_credentials(body: str) -> None:
    assert _redacted_error(body=body).message == body


@pytest.mark.parametrize(
    "body",
    [
        "https://gw.example/v1?filter[a]=1&sig=SIGSECRET",
        "https://gw.example/v1?fields=a,b&sig=SIGSECRET",
        "https://gw.example/v1?q=(a)&sig=SIGSECRET",
        "https://user:pw@/x?token=SECRET",
    ],
)
def test_error_redactor_strips_complete_credential_bearing_urls(body: str) -> None:
    error = _redacted_error(body=body)

    assert "SIGSECRET" not in error.message
    assert "SECRET" not in error.message
    assert "user:pw@" not in error.message


def test_structured_json_redaction_preserves_sibling_fields() -> None:
    error = _redacted_error(
        body='{"error":{"type":"invalid_request_error","api_key":"zzz","param":"model"}}'
    )

    assert error.message == (
        '{"error":{"type":"invalid_request_error","api_key":"[redacted]","param":"model"}}'
    )


def test_configured_literal_is_replaced_before_token_shape_redaction() -> None:
    from core.agent_core.ai.errors import redact_provider_text

    redacted = redact_provider_text(
        r"Illegal header value b'opaque.private~value\n'",
        literal_secrets=("opaque.private~value\n",),
    )

    assert redacted == "Illegal header value b'[redacted]'"


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("request at https://x.example/chat. retry", "request at https://x.example/chat. retry"),
        ("request (https://x.example/chat?sig=secret).", "request (https://x.example/chat)."),
        (
            json.dumps({"detail": '"https://x.example/y?sig=secret"'}),
            json.dumps({"detail": '"https://x.example/y"'}, separators=(",", ":")),
        ),
    ],
)
def test_diagnostic_url_redaction_preserves_surrounding_syntax(
    diagnostic: str, expected: str,
) -> None:
    from core.agent_core.ai._common import StreamAssembler

    assembler = StreamAssembler(origin=Origin("p", "openai_chat", "m"), protocol="openai_chat")
    error = assembler.terminal(assembler.error(diagnostic, kind="invalid_request"))
    assert error.message == expected


def test_endpoint_is_sanitized_before_diagnostic_redaction() -> None:
    endpoint = "https://gw.example/v1?key=K&sig=SIGSECRET&x=1"
    from core.agent_core.ai._common import StreamAssembler

    assembler = StreamAssembler(
        origin=Origin("openai", "openai_chat", "model"),
        protocol="openai_chat",
        endpoint_url=endpoint,
    )
    error = assembler.terminal(assembler.error(
        f"request failed at {endpoint}/chat/completions",
        kind="invalid_request",
    ))

    assert "SIGSECRET" not in error.message
    assert "x=1" not in error.message
    assert "https://gw.example/v1/chat/completions" in error.message


@pytest.mark.parametrize(
    ("configured", "diagnostic"),
    [
        (
            "plainSECRETvalue123",
            "Illegal header value b'Bearer plainSECRETvalue123\\n'",
        ),
        (
            "TOKENSECRET77\n",
            "Illegal header value b'Bearer TOKENSECRET77\\n'",
        ),
    ],
)
def test_boundary_redacts_configured_request_credentials_in_transport_errors(
    configured: str,
    diagnostic: str,
) -> None:
    from core.agent_core.ai._common import StreamAssembler

    assembler = StreamAssembler(
        origin=Origin("openai", "openai_chat", "model"),
        protocol="openai_chat",
        sensitive_values=(configured,),
    )
    error = assembler.terminal(assembler.exception(ValueError(diagnostic)))

    assert configured.strip() not in error.message
    assert "Bearer [redacted]" in error.message


def test_request_credential_values_uses_shared_sensitive_key_vocabulary() -> None:
    from core.agent_core.ai._common import request_credential_values
    from core.agent_core.ai.provider import ModelEndpoint

    endpoint = ModelEndpoint(
        protocol="openai_chat",
        base_url="https://gateway.example/v1",
        model_id="model",
        token="endpoint-token-value",
        request_headers={
            "Ocp-Apim-Subscription-Key": "subscription-value",
            "X-Credential": "credential-value",
            "X-Private-Key": "private-value",
        },
    )

    assert request_credential_values(endpoint) == (
        "endpoint-token-value",
        "subscription-value",
        "credential-value",
        "private-value",
    )


def test_short_configured_placeholders_do_not_redact_diagnostic_text() -> None:
    from core.agent_core.ai.errors import redact_provider_text

    assert redact_provider_text(
        "model none-large uses token budget",
        literal_secrets=("token", "key", "none"),
    ) == "model none-large uses token budget"


@pytest.mark.parametrize("source", _PROTOCOL_ORIGINS)
@pytest.mark.parametrize("target", _PROTOCOL_ORIGINS)
def test_history_from_each_protocol_can_be_replayed_without_foreign_opaque_state(
    source: Origin,
    target: Origin,
) -> None:
    call_id = "native|call"
    history = (
        UserMessage((TextBlock(text="question"),)),
        AssistantMessage(
            content=(
                ThinkingBlock("private", "thinking-signature"),
                ToolCallBlock(call_id, "read", {"path": "x"}, signature="tool-signature"),
            ),
            origin=source,
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            tool_call_id=call_id,
            tool_name="read",
            content=(TextBlock(text="result"),),
        ),
    )

    transformed = transform_messages(history, target, supports_images=True)
    assistant = next(message for message in transformed.messages if isinstance(message, AssistantMessage))
    result = next(message for message in transformed.messages if isinstance(message, ToolResultMessage))
    assert result.tool_call_id == assistant.tool_calls[0].id
    assert any(isinstance(message, ToolResultMessage) for message in transformed.messages)
    if source == target:
        assert assistant.content[0] == ThinkingBlock("private", "thinking-signature")
        assert assistant.tool_calls[0].signature == "tool-signature"
    else:
        assert all(
            not isinstance(block, ThinkingBlock) or block.signature is None
            for block in assistant.content
        )
        assert assistant.tool_calls[0].signature is None
