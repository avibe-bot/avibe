"""What a Discord message Avibe sends may notify (#2673): a user it names, never @everyone, @here or a role."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from discord import AllowedMentions
from discord.http import handle_message_parameters

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import DiscordConfig
from modules.im.discord import DiscordBot


@pytest.mark.parametrize("per_send", [None, ...])
def test_a_send_notifies_only_the_users_it_names(per_send) -> None:
    """Every send, edit and interaction reply merges the client's ``allowed_mentions`` with its own, as
    ``Messageable.send`` does here, and no send of ours passes its own. Text an agent or a model wrote
    (``@everyone``, ``@here``, ``<@&role>``) therefore stays inert, while ``<@user>`` still notifies."""

    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    params = handle_message_parameters(
        content="@everyone @here <@&1234> <@5678> the build is green",
        **({} if per_send is ... else {"allowed_mentions": per_send}),
        previous_allowed_mentions=bot.client.allowed_mentions,
    )
    assert params.payload["allowed_mentions"]["parse"] == ["users"]


def test_a_recreated_client_keeps_the_rule() -> None:
    """The runtime builds a fresh client after a failed start; it must not fall back to resolving everything."""

    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    fresh = bot._new_client()
    assert isinstance(fresh.allowed_mentions, AllowedMentions)
    assert fresh.allowed_mentions.to_dict()["parse"] == ["users"]
