"""Deep links back to the message that created a Harness definition.

One function, one rule: LINK OR ``None``, never a fabricated URL. A failure notice
that offers a link the user cannot follow is worse than a notice that offers none —
it moves the failure from "I do not know where this came from" to "Avibe told me
where and it was wrong", and the second is the one that costs trust.

So every branch below refuses unless it holds EVERY id that platform's permalink
grammar requires, and the honesty table is the contract:

============  ================================================  =====================
platform      link when                                          ``None`` when
============  ================================================  =====================
``slack``     channel id + message ts                            either is missing
``discord``   workspace (guild) id + channel id + message id     any is missing —
                                                                 notably a DM, which
                                                                 has no guild
``telegram``  a ``-100…`` supergroup/channel id + message id     a private/basic-group
                                                                 chat id, or no
                                                                 message id
``lark``      never                                              always
``wechat``    never                                              always
``avibe``     never                                              always
unknown       never                                              always
============  ================================================  =====================

The three refusals-by-design are refusals of DIFFERENT kinds and are spelled out
rather than lumped together:

* Feishu/Lark (wire value ``lark``) and WeChat have no stable public message
  permalink an external notice can address, so there is nothing to build.
* ``avibe`` is the Workbench itself. It HAS an addressable location, but the only URL
  form is the local setup host — and a ``http://127.0.0.1:<port>/…`` link pasted into
  a Slack DM or a phone push is not honestly reachable from where the notice is
  read. The origin TEXT still names the Workbench; only the link is withheld.
* An unrecognised platform is a wire value this module has never seen. Guessing a URL
  shape for it is the fabrication this whole module exists to prevent.
"""

from __future__ import annotations

from typing import Optional


def _clean(value: object) -> str:
    return str(value or "").strip()


def _slack_link(channel_id: str, thread_id: str, message_id: str) -> Optional[str]:
    if not channel_id or not message_id:
        return None
    # ``1710000000.000100`` → ``p1710000000000100``: Slack's archive path is the ts
    # with the dot removed, and nothing else about it is transformed.
    link = f"https://slack.com/archives/{channel_id}/p{message_id.replace('.', '')}"
    if thread_id and thread_id != message_id:
        # A REPLY. Without these params the link opens the parent channel at the
        # message; with them it opens the thread the definition was actually created
        # in. ``thread_id == message_id`` is Slack's own "not in a thread" encoding
        # (the adapter defaults ``thread_id`` to the message's own ts), so it adds
        # nothing there and is left off.
        link = f"{link}?thread_ts={thread_id}&cid={channel_id}"
    return link


def _discord_link(workspace_id: str, channel_id: str, message_id: str) -> Optional[str]:
    if not workspace_id or not channel_id or not message_id:
        # NEVER ``@me``. That path is valid Discord syntax and resolves for the ONE
        # user whose DM it is, so it would read as a working link while being
        # unfollowable for anyone else — including the workspace inbox, which is not a
        # person at all.
        return None
    return f"https://discord.com/channels/{workspace_id}/{channel_id}/{message_id}"


def _telegram_link(channel_id: str, message_id: str) -> Optional[str]:
    # ``t.me/c/<internal id>/<message id>`` exists only for supergroups and channels,
    # whose ids Telegram prefixes with ``-100``. A private chat or a basic group has no
    # such link at all, and stripping four characters off an id that does not carry
    # that prefix would address an unrelated chat.
    if not message_id or not channel_id.startswith("-100"):
        return None
    internal_id = channel_id[4:]
    if not internal_id:
        return None
    return f"https://t.me/c/{internal_id}/{message_id}"


def origin_link(
    platform: object,
    channel_id: object,
    thread_id: object,
    message_id: object,
    workspace_id: object,
) -> Optional[str]:
    """A followable URL for the creating message, or ``None``.

    Pure: no config, no network, no database. Every argument is an id captured at
    definition-creation time (``CallerContext``'s origin fields), so this can be called
    from a notice body, a test, or an ad-hoc probe with the same result.
    """

    resolved_platform = _clean(platform)
    channel = _clean(channel_id)
    message = _clean(message_id)
    if resolved_platform == "slack":
        return _slack_link(channel, _clean(thread_id), message)
    if resolved_platform == "discord":
        return _discord_link(_clean(workspace_id), channel, message)
    if resolved_platform == "telegram":
        return _telegram_link(channel, message)
    return None
