from __future__ import annotations

import pytest

from core.caller_context import (
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_CALLER_CHANNEL_ID_ENV,
    AVIBE_CALLER_MESSAGE_ID_ENV,
    AVIBE_CALLER_PLATFORM_ENV,
    AVIBE_CALLER_SESSION_KEY_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_CALLER_USER_ID_ENV,
    AVIBE_CALLER_WORKSPACE_ID_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
    caller_context_from_env,
    caller_context_from_platform_payload,
)


def test_caller_context_from_env_requires_session_id() -> None:
    assert caller_context_from_env({}) is None


def test_caller_context_from_env_round_trips_metadata_and_env() -> None:
    context = caller_context_from_env(
        {
            AVIBE_SESSION_ID_ENV: "ses123",
            AVIBE_RUN_ID_ENV: "run456",
            AVIBE_CALLER_SOURCE_ENV: "agent_run",
            AVIBE_CALLER_BACKEND_ENV: "codex",
            AVIBE_NATIVE_SESSION_ID_ENV: "thread789",
        }
    )

    assert context is not None
    assert context.to_metadata() == {
        "session_id": "ses123",
        "run_id": "run456",
        "source": "agent_run",
        "backend": "codex",
        "native_session_id": "thread789",
    }
    caller_env = context.to_env()
    assert caller_env[AVIBE_SESSION_ID_ENV] == "ses123"
    assert "PATH" not in caller_env


def test_caller_context_from_platform_payload_prefers_agent_session_target() -> None:
    context = caller_context_from_platform_payload(
        {
            "agent_session_id": "legacy",
            "task_execution_id": "run123",
            "task_trigger_kind": "agent_run",
            "agent_session_target": {
                "id": "ses-target",
                "agent_backend": "opencode",
                "native_session_id": "oc-session",
            },
        }
    )

    assert context is not None
    assert context.to_metadata() == {
        "session_id": "ses-target",
        "run_id": "run123",
        "source": "agent_run",
        "backend": "opencode",
        "native_session_id": "oc-session",
    }
    assert context.to_env()[AVIBE_NATIVE_SESSION_ID_ENV] == "oc-session"


def test_caller_context_from_platform_payload_preserves_callback_source() -> None:
    context = caller_context_from_platform_payload(
        {
            "agent_session_id": "ses-callback",
            "task_execution_id": "run-callback",
            "task_trigger_kind": "agent_run",
            "source_kind": "callback",
        }
    )

    assert context is not None
    assert context.source == "callback"


# --- the creation origin ---------------------------------------------------
#
# Subordinate to HFR-094's notice-body family (round 14 gate item 3, review comment
# 5121007240) — no new scenario id. These fields exist so a failure notice can name the
# conversation a definition was created in; the copy that renders them is covered in
# ``tests/test_harness_failure_visibility.py``.


def _slack_channel_context():
    """A Slack channel message, shaped exactly as ``modules/im/slack.py`` builds it.

    Note what the adapter does NOT set: ``platform`` is absent from both the typed
    context and ``platform_specific``, which is why every capture call site has to pass
    a ``fallback_platform`` resolved the same way the rest of the handler resolves it.
    """

    from modules.im import MessageContext

    return MessageContext(
        user_id="U0AUTHOR",
        channel_id="C0123",
        thread_id="1710000000.000100",
        message_id="1710000000.000100",
        platform_specific={"team_id": "T0999", "is_dm": False, "event": {}},
    )


def _agent_turn_payload(extra: dict | None = None) -> dict:
    payload = {
        "agent_session_id": "ses123",
        "task_execution_id": "run456",
        "task_trigger_kind": "agent_turn",
    }
    payload.update(extra or {})
    return payload


def test_the_creation_origin_survives_the_env_hop_into_created_by_caller() -> None:
    """The whole contract in one pass: IM turn → env → subprocess → persisted metadata.

    THE DEFECT this pins. An IM-created Harness definition is created by an Agent run
    executing ``vibe task add``, so the ONLY channel between the conversation that asked
    for it and the ``created_by.caller`` row that records it is the subprocess env. The
    capture dropped every IM id on the floor: ``CallerContext`` carried session/run/
    source/backend ids and nothing about where the request came from. Two consequences,
    both closed here — the failure ladder's rungs (3) and (4) read
    ``caller["session_key"]`` / ``caller["platform"]`` / ``caller["user_id"]``, fields
    nothing had ever written, so the owner-DM rung was dead code; and the notice body
    had no origin to name.

    RED at 3578f2b6 (round 11) as an overlay spelling the env vars as string literals
    rather than importing the new constants — an ``ImportError`` is not a red. There the
    metadata came back as ``{'session_id': 'ses123', 'run_id': 'run456', 'source':
    'agent_turn'}``: no ``platform``, no ``user_id``, no ``session_key``, no
    ``channel_id``, no ``message_id``, no ``workspace_id``.

    The env is asserted as the MIDDLE of the hop, not as the destination: values are
    read back through ``caller_context_from_env`` and only then compared, because a
    field that serialises but does not deserialise is exactly as broken as one that was
    never captured.
    """

    from vibe.cli import _definition_creation_metadata_from_caller

    captured = caller_context_from_platform_payload(
        _agent_turn_payload({"team_id": "T0999", "is_dm": False, "event": {}}),
        message=_slack_channel_context(),
        fallback_platform="slack",
    )
    assert captured is not None

    env = captured.to_env()
    assert env[AVIBE_CALLER_PLATFORM_ENV] == "slack"
    assert env[AVIBE_CALLER_USER_ID_ENV] == "U0AUTHOR"
    assert env[AVIBE_CALLER_CHANNEL_ID_ENV] == "C0123"
    assert env[AVIBE_CALLER_SESSION_KEY_ENV] == "slack::channel::C0123::thread::1710000000.000100"
    assert env[AVIBE_CALLER_MESSAGE_ID_ENV] == "1710000000.000100"
    assert env[AVIBE_CALLER_WORKSPACE_ID_ENV] == "T0999"

    # The subprocess hop: a fresh process sees only this mapping.
    rehydrated = caller_context_from_env(env)
    assert rehydrated is not None
    assert rehydrated == captured, (
        "every origin field has to round-trip; one that serialises without "
        "deserialising is as broken as one never captured"
    )

    # And the REAL writer — the function ``vibe task add`` uses to build the row.
    metadata = _definition_creation_metadata_from_caller(rehydrated)
    caller = metadata["created_by"]["caller"]
    assert caller["platform"] == "slack", "rung (4) reads exactly this key"
    assert caller["user_id"] == "U0AUTHOR", "and exactly this one"
    assert caller["session_key"] == "slack::channel::C0123::thread::1710000000.000100", (
        "rung (3) reads exactly this key"
    )
    assert caller["scope_id"] == "slack::channel::C0123", (
        "the thread key's parent scope, which rung (3) falls back to and "
        "``parse_scope_id`` cannot express from the five-part form"
    )
    assert caller["channel_id"] == "C0123"
    assert caller["message_id"] == "1710000000.000100"
    assert caller["workspace_id"] == "T0999"


def test_a_non_thread_session_key_records_no_redundant_scope_id() -> None:
    """``scope_id`` is written only when it says something ``session_key`` does not.

    Rung (3) reads ``session_key or scope_id``, so a copy of the same value would be
    dead weight in every persisted definition. The three-part form IS its own scope.
    """

    from modules.im import MessageContext

    captured = caller_context_from_platform_payload(
        _agent_turn_payload({"is_dm": False}),
        message=MessageContext(user_id="U1", channel_id="C1", platform="discord"),
    )
    assert captured is not None
    assert captured.session_key == "discord::channel::C1"
    assert "scope_id" not in captured.to_metadata()


@pytest.mark.parametrize(
    "platform,payload_extra,kwargs,why",
    [
        (
            "slack",
            {"team_id": "T0999", "is_dm": False},
            {"thread_id": "1710000000.000100"},
            "a Slack channel message, thread ts included",
        ),
        (
            "slack",
            {"is_dm": True},
            {"channel_id": "D0123", "thread_id": "1710000000.000100"},
            "a Slack DM, whose scope id is the USER not the D-channel",
        ),
        (
            "telegram",
            {"is_dm": False, "is_forum": True},
            {"platform": "telegram", "channel_id": "-1001234567890", "thread_id": None},
            "a Telegram forum with no explicit topic id, which canonicalises to '1'",
        ),
        (
            "telegram",
            {"is_dm": False, "is_topic_message": True},
            {"platform": "telegram", "channel_id": "-1001234567890", "thread_id": "77"},
            "a Telegram topic message with an explicit topic id",
        ),
        (
            "telegram",
            {"is_dm": True},
            {"platform": "telegram", "channel_id": "42", "thread_id": None},
            "a Telegram DM, which has no thread segment at all",
        ),
        (
            # The asymmetry the mirror has to reproduce rather than improve on:
            # ``build_session_key_for_context`` applies ``fallback_platform`` to the
            # key's PREFIX but calls ``resolve_context_thread_id`` without it, so a
            # forum flag on a context whose own ``platform`` is unset yields NO thread
            # segment. Unreachable in production (the Telegram adapter always sets
            # ``platform``), and the case that caught this implementation canonicalising
            # a thread the rest of the system would not have addressed.
            "telegram",
            {"is_dm": False, "is_forum": True},
            {"channel_id": "-1001234567890", "thread_id": None},
            "a forum flag with no platform on the context itself",
        ),
        (
            "discord",
            {"is_dm": False},
            {"platform": "discord", "channel_id": "555", "thread_id": "778"},
            "a Discord thread",
        ),
        (
            "lark",
            {"is_dm": False},
            {"platform": "lark", "channel_id": "oc_abc", "thread_id": None},
            "a Lark group, whose origin is nameable even though it is not linkable",
        ),
    ],
)
def test_the_captured_origin_session_key_matches_the_canonical_builder(
    platform: str,
    payload_extra: dict,
    kwargs: dict,
    why: str,
) -> None:
    """Drift pin for the one piece of logic this module reimplements.

    The origin session key has to be the SAME grammar ``build_session_key_for_context``
    produces, because rung (3) delivers to it — a notice addressed to a key the rest of
    the system would not have built is a notice sent to the wrong conversation. The rule
    is reimplemented rather than imported so ``core/caller_context.py`` keeps no
    dependency on ``modules.im`` (the CLI imports it on every invocation), and this test
    is what makes the duplication safe. If ``build_session_key_for_context`` changes,
    this fails.
    """

    from core.scheduled_tasks import build_session_key_for_context
    from modules.im import MessageContext

    context = MessageContext(
        user_id=kwargs.get("user_id", "U0AUTHOR"),
        channel_id=kwargs.get("channel_id", "C0123"),
        platform=kwargs.get("platform"),
        thread_id=kwargs.get("thread_id"),
        message_id="m1",
        platform_specific=dict(payload_extra),
    )
    canonical = build_session_key_for_context(
        context,
        include_thread=True,
        fallback_platform=platform,
    ).to_key()

    captured = caller_context_from_platform_payload(
        _agent_turn_payload(payload_extra),
        message=context,
        fallback_platform=platform,
    )
    assert captured is not None
    assert captured.session_key == canonical, f"drift on {why}"


def test_a_discord_dm_captures_no_workspace_id() -> None:
    """``message.guild`` is ``None`` in a DM, and a missing tenant must stay missing.

    Read defensively off whatever the adapter already put on the payload — no
    ``modules/im`` change feeds this — and the absence is what makes
    ``origin_link`` refuse rather than fall back to Discord's ``@me`` path.
    """

    from types import SimpleNamespace

    from modules.im import MessageContext

    guild_message = SimpleNamespace(guild=SimpleNamespace(id=999))
    in_guild = caller_context_from_platform_payload(
        _agent_turn_payload({"message": guild_message, "is_dm": False}),
        message=MessageContext(user_id="U1", channel_id="555", platform="discord"),
    )
    assert in_guild is not None
    assert in_guild.workspace_id == "999", "an int guild id is captured as its string form"

    in_dm = caller_context_from_platform_payload(
        _agent_turn_payload({"message": SimpleNamespace(guild=None), "is_dm": True}),
        message=MessageContext(user_id="U1", channel_id="U1", platform="discord"),
    )
    assert in_dm is not None
    assert in_dm.workspace_id is None


def test_a_caller_with_no_message_context_captures_no_origin() -> None:
    """The CLI-only caller, unchanged.

    ``vibe task add`` run by hand has no conversation behind it, so it must keep
    producing exactly the pre-origin env and metadata — not empty origin fields, which
    would render an origin line about nothing.
    """

    context = caller_context_from_platform_payload(_agent_turn_payload())
    assert context is not None
    assert context.to_metadata() == {
        "session_id": "ses123",
        "run_id": "run456",
        "source": "agent_turn",
    }
    assert set(context.to_env()) == {
        AVIBE_SESSION_ID_ENV,
        AVIBE_RUN_ID_ENV,
        AVIBE_CALLER_SOURCE_ENV,
    }


def test_a_session_scoped_caller_env_drops_only_the_per_turn_origin() -> None:
    """The asymmetry between backends that CAN refresh their caller env and one that cannot.

    The Claude SDK client is spawned once per session with a fixed environment, and that
    same environment is the value compared to decide whether a cached client may be
    reused. Two fields make that combination unsafe, for two different reasons, and both
    are worse than the information they carry:

    * ``message_id`` changes every turn, so leaving it in would respawn the Agent on
      every single message;
    * ``user_id`` changes with the author, and a channel session is deliberately shared
      across participants — baking in the first speaker would later attribute another
      participant's definition to them, and the owner-DM rung would notify the wrong
      person.

    Everything the SESSION owns survives, so a Claude-created definition still names its
    conversation and still lights up rung (3). It gets no deep link, because every
    permalink grammar needs the message id — an omission, not a wrong URL. Codex
    (``BASH_ENV`` script) and OpenCode (binding file) rewrite their env per turn and keep
    the full origin, which is why this is a per-call-site flag and not a global policy.
    """

    captured = caller_context_from_platform_payload(
        _agent_turn_payload({"team_id": "T0999", "is_dm": False, "event": {}}),
        message=_slack_channel_context(),
        fallback_platform="slack",
    )
    assert captured is not None

    stable = captured.session_stable()
    assert stable.user_id is None and stable.message_id is None
    assert (stable.platform, stable.channel_id, stable.workspace_id) == ("slack", "C0123", "T0999")
    assert stable.session_key == "slack::channel::C0123::thread::1710000000.000100", (
        "the conversation is session-owned and must survive"
    )
    assert stable.session_id == captured.session_id

    assert AVIBE_CALLER_USER_ID_ENV not in stable.to_env()
    assert AVIBE_CALLER_MESSAGE_ID_ENV not in stable.to_env()
    assert AVIBE_CALLER_SESSION_KEY_ENV in stable.to_env()

    from core.caller_context import caller_env_for_platform_payload

    payload = _agent_turn_payload({"team_id": "T0999", "is_dm": False})
    full = caller_env_for_platform_payload(
        payload, message=_slack_channel_context(), fallback_platform="slack"
    )
    scoped = caller_env_for_platform_payload(
        payload,
        message=_slack_channel_context(),
        fallback_platform="slack",
        session_stable_only=True,
    )
    assert set(full) - set(scoped) == {AVIBE_CALLER_USER_ID_ENV, AVIBE_CALLER_MESSAGE_ID_ENV}


def test_a_dm_loses_nothing_to_the_session_scoped_form() -> None:
    """The case where the person IS the scope.

    A DM's session key is ``<platform>::user::<id>``, so dropping ``user_id`` costs the
    owner-DM rung nothing: rung (3) already addresses that same person. The stable form
    is only lossy for a SHARED conversation, which is exactly where sharing is correct.
    """

    from modules.im import MessageContext

    captured = caller_context_from_platform_payload(
        _agent_turn_payload({"is_dm": True}),
        message=MessageContext(
            user_id="U0AUTHOR",
            channel_id="D0123",
            platform="slack",
            message_id="1710000000.000200",
        ),
    )
    assert captured is not None
    assert captured.session_stable().session_key == "slack::user::U0AUTHOR"
