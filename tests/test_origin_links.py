"""The honesty table for creation-origin deep links (``core/origin_links.py``).

Subordinate to HFR-094's notice-body family — no new scenario id this round; the
localized notice-context contract these links serve is round 14's gate item 3 (review
comment 5121007240).

ONE RULE, and every test below is a case of it: a link is offered only when every id
that platform's permalink grammar requires was actually captured. The failure mode this
guards is not a missing link — it is a PRESENT one that does not resolve, which converts
"I cannot tell where this came from" into "Avibe told me where and it was wrong".

There is deliberately no red baseline for this file. It characterises a new pure
module, so at any earlier commit it would only raise ``ImportError``, and an
``ImportError`` is not evidence of a defect. The reds that ARE evidence live with the
capture round-trip (``tests/test_caller_context.py``) and the rendered body
(``tests/test_harness_failure_visibility.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.failure_notices import NOTICE_ORIGIN_PLATFORM_I18N_KEYS
from core.origin_links import origin_link

SLACK_TS = "1710000000.000100"
SLACK_PARENT_TS = "1709999999.000900"


def test_a_slack_channel_message_links_to_its_archive_path() -> None:
    """The ts loses its dot and nothing else about it is transformed."""

    assert origin_link("slack", "C0123", SLACK_TS, SLACK_TS, "T0999") == (
        "https://slack.com/archives/C0123/p1710000000000100"
    )


def test_a_slack_thread_reply_links_into_the_thread_not_the_channel() -> None:
    """``thread_ts``/``cid`` are what make the link open the thread it was created in.

    The Slack adapter defaults a non-threaded message's ``thread_id`` to the message's
    own ts, so ``thread_id != message_id`` is the platform's own encoding of "this is a
    reply" — and the query params are added on exactly that condition rather than
    whenever a thread id happens to be present.
    """

    link = origin_link("slack", "C0123", SLACK_PARENT_TS, SLACK_TS, "T0999")
    assert link == (
        "https://slack.com/archives/C0123/p1710000000000100"
        f"?thread_ts={SLACK_PARENT_TS}&cid=C0123"
    )

    assert "?" not in str(origin_link("slack", "C0123", SLACK_TS, SLACK_TS, "T0999")), (
        "a top-level message must not be dressed up as a thread reply"
    )


@pytest.mark.parametrize(
    "channel_id,message_id,missing",
    [
        ("", SLACK_TS, "the channel"),
        ("C0123", "", "the message ts"),
        ("", "", "both ids"),
    ],
)
def test_slack_refuses_without_both_ids(channel_id: str, message_id: str, missing: str) -> None:
    assert origin_link("slack", channel_id, None, message_id, "T0999") is None, (
        f"a Slack archive path cannot be built without {missing}"
    )


def test_a_discord_guild_message_links_to_the_three_part_channel_path() -> None:
    assert origin_link("discord", "555", None, "777", "999") == (
        "https://discord.com/channels/999/555/777"
    )


def test_discord_refuses_a_dm_rather_than_addressing_it_as_me() -> None:
    """``@me`` is valid Discord syntax and is exactly why it must not be used.

    A ``channels/@me/<c>/<m>`` path resolves for the one user whose DM it is, so it
    would read as a working link while being unfollowable for anybody else — including
    the workspace inbox, which is not a person at all. A DM has no guild
    (``message.guild is None``), so the honest answer is no link.
    """

    link = origin_link("discord", "555", None, "777", None)
    assert link is None, f"a guild-less Discord origin gets no link: {link}"
    assert "@me" not in str(link)


@pytest.mark.parametrize(
    "channel_id,message_id",
    [("", "777"), ("555", "")],
)
def test_discord_refuses_without_channel_and_message(channel_id: str, message_id: str) -> None:
    assert origin_link("discord", channel_id, None, message_id, "999") is None


def test_a_telegram_supergroup_message_links_through_the_internal_id() -> None:
    """``t.me/c/<id without the -100 prefix>/<message id>``."""

    assert origin_link("telegram", "-1001234567890", None, "42", None) == (
        "https://t.me/c/1234567890/42"
    )


@pytest.mark.parametrize(
    "channel_id,why",
    [
        ("123456789", "a private chat id has no t.me/c form at all"),
        ("-123456789", "a basic group id is not a -100 supergroup id"),
        ("-100", "the prefix alone leaves no internal id"),
        ("", "no chat id was captured"),
    ],
)
def test_telegram_refuses_any_chat_without_the_supergroup_prefix(channel_id: str, why: str) -> None:
    """Stripping four characters off an id that never carried the prefix would address
    an UNRELATED chat, which is the worst outcome available here."""

    assert origin_link("telegram", channel_id, None, "42", None) is None, why


def test_telegram_refuses_without_a_message_id() -> None:
    assert origin_link("telegram", "-1001234567890", None, "", None) is None


@pytest.mark.parametrize("platform", ["lark", "wechat"])
def test_platforms_with_no_public_permalink_get_no_link(platform: str) -> None:
    """Feishu/Lark and WeChat expose no stable message permalink an external notice can
    address, so there is nothing to build — the origin TEXT still names them."""

    assert origin_link(platform, "oc_abc", None, "om_abc", "tenant") is None


def test_a_workbench_origin_gets_no_link_even_though_it_has_a_location() -> None:
    """The refusal that is about REACHABILITY rather than about a missing id.

    An ``avibe`` origin is the Workbench, which is addressable — but only as the local
    setup host, and a ``http://127.0.0.1:<port>/…`` URL pasted into a Slack DM or a
    phone push notification is not honestly followable from where the notice is read.
    """

    assert origin_link("avibe", "proj-1", None, "msg-1", None) is None


@pytest.mark.parametrize("platform", ["", None, "mystery_platform", "SLACK"])
def test_an_unrecognised_platform_never_gets_a_guessed_url(platform) -> None:
    """Including the case-mismatched value: wire values are matched exactly, because a
    permalink grammar is not something to infer from a near-miss."""

    assert origin_link(platform, "C0123", None, SLACK_TS, "T0999") is None


def test_every_linkable_platform_is_a_platform_the_notice_can_also_name() -> None:
    """Drift pin between the two closed maps.

    A platform that produces a link but has no display label would render a bare URL
    with no sentence around it; a label with no link is fine and expected (Lark, WeChat,
    the Workbench). So the link builder's vocabulary must be a SUBSET of the notice's.
    """

    linkable = {"slack", "discord", "telegram"}
    assert linkable <= set(NOTICE_ORIGIN_PLATFORM_I18N_KEYS), (
        "a platform that can be linked must also be nameable in the notice copy"
    )
    for platform in linkable:
        assert origin_link(platform, "c", None, "m", "w") is not None or platform == "telegram", (
            f"{platform} must be able to produce a link given a full id set"
        )
