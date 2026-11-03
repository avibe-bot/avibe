"""Bots: an enabled Vibey Agent plus one permanent Session and a working directory (``docs/plans/bots.md``).

A Bot is no new table. Its fields live in its Agent's metadata under ``bot`` (D1), a key ordinary Agent create and
update can neither set nor clear (``core.vibe_agents.STORE_OWNED_METADATA_KEYS``); this module is its one writer.

- **Handle** (D2): lowercase ASCII, frozen once the Bot exists. It names the Session (``ses_<handle>``), the working
  directory (``~/.avibe/bots/<handle>/``), and every ``@<bot:handle>`` marker and ``--bot`` invocation.
- **Removing** the Bot role keeps the Agent, the Session, and the directory, hidden; the record keeps its handle with
  ``removed_at``, so setting the Agent as a Bot again reconnects them.
- **The main Bot** (D9) is the store-made ``main`` Agent (``VibeAgentStore.ensure_main_bot_agent``); it is always a
  Bot, and its Session is the released ``ses_main``.
- Bots are the instance Owner's (D1): every operation here requires the Owner.
- **Hide details** (D5): whether the Bot's conversation hides its execution process, Harness messages, and
  background tasks, so it reads like talking to a person. Kept on the Bot so every device agrees; on when absent.
- **Avatars** (D5): a custom Bot shows an emoji on a hue, an uploaded image, or its name's first character. The main
  Bot always shows the Vibey mascot (owner decision, 2026-11-03): changing it is refused, and an avatar stored before
  that is ignored, not cleared, so no migration rewrites the record.
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection

from config import paths
from core.vibe_agents import (
    BOT_METADATA_KEY,
    MAIN_BOT_HANDLE,
    VibeAgent,
    VibeAgentStore,
    agent_bot_record,
    is_builtin_default_agent,
    is_main_bot_agent,
    normalize_agent_name,
)
from core.inbox_events import publish_bots_changed
from storage.agent_session_rows import BOT_SESSION_ID_PREFIX, is_bot_session_id, reserve_write_lock
from storage.agent_session_rows import hidden_bot_session_ids as stored_hidden_bot_session_ids
from storage.models import agent_sessions, agents
from vibe.authorization import AuthorizationContext, require_instance_role

logger = logging.getLogger(__name__)

BOT_BACKEND = "vibey"
BOT_HANDLE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
BOT_HANDLE_MAX_LENGTH = 24
#: Handles kept for the built-in Bots: the main Bot, and later the memory and operations Bots (D2).
RESERVED_BOT_HANDLES = frozenset({MAIN_BOT_HANDLE, "memory", "ops"})
DISPLAY_NAME_MAX_LENGTH = 64
AVATAR_FILENAME = "avatar.png"
AVATAR_SIZE = 256
AVATAR_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
#: Code points in one emoji avatar. The longest RGI sequences (a kiss or a couple with two skin tones, such as
#: 🧑🏻‍❤️‍💋‍🧑🏼) have 10, a subdivision flag 7; the rest leaves room for longer sequences Unicode may add.
EMOJI_MAX_LENGTH = 16
HANDLE_SOURCE_MODEL = "model"
HANDLE_SOURCE_TRANSLITERATION = "transliteration"
GIT_INIT_TIMEOUT_SECONDS = 30


class BotError(ValueError):
    """A refused Bot operation: a stable ``code`` for every surface, the HTTP status it maps to, and details."""

    def __init__(self, code: str, message: str, *, status: int = 400, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _not_found(handle: str) -> BotError:
    return BotError("bot_not_found", f"Bot '{handle}' not found", status=404, handle=handle)


def _main_avatar_locked() -> BotError:
    return BotError("bot_main_avatar", "The main Bot always shows the Vibey mascot; its avatar cannot be changed",
                    status=409)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- identity ---------------------------------------------------------------------------------------------------


def bot_session_id(handle: str) -> str:
    return f"{BOT_SESSION_ID_PREFIX}{handle}"


def bot_handle_for_session_id(session_id: Any) -> Optional[str]:
    """The handle a Bot-namespace Session id names, or None for any other Session."""
    text = str(session_id or "")
    if not is_bot_session_id(text):
        return None
    handle = text[len(BOT_SESSION_ID_PREFIX):]
    return handle if BOT_HANDLE_PATTERN.fullmatch(handle) else None


def bot_directory(handle: str) -> Path:
    return paths.get_bot_dir(handle)


def is_valid_handle(handle: Any) -> bool:
    """D2's pattern, and no trailing hyphen: a new Bot's Agent is named after its handle, and an Agent name never
    ends in one, so such a handle could never be created."""
    return isinstance(handle, str) and BOT_HANDLE_PATTERN.fullmatch(handle) is not None and not handle.endswith("-")


def normalize_handle(text: Any) -> Optional[str]:
    """``text`` as a handle: lowercase ASCII letters, digits, and single hyphens, starting with a letter."""
    lowered = str(text or "").lower()
    candidate = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    candidate = re.sub(r"^[^a-z]+", "", candidate)
    candidate = candidate[:BOT_HANDLE_MAX_LENGTH].rstrip("-")
    return candidate if is_valid_handle(candidate) else None


def ascii_name_handle(display_name: str) -> Optional[str]:
    """D2 step 1: an ASCII display name, normalized (``Researcher`` to ``researcher``)."""
    name = str(display_name or "").strip()
    return normalize_handle(name) if name.isascii() else None


def transliterated_handle(display_name: str) -> Optional[str]:
    """D2 step 3: the display name transliterated with ``anyascii`` (``小爱`` to ``xiaoai``)."""
    from anyascii import anyascii

    return normalize_handle(anyascii(str(display_name or "")))


def model_handle(text: Any) -> Optional[str]:
    """A model's proposal, accepted only if it is a handle once trimmed of quoting (``CSO`` reads as ``cso``)."""
    candidate = str(text or "").strip().strip("`'\"").strip().removeprefix("@").lower()
    return candidate if is_valid_handle(candidate) else None


def _with_suffix(base: str, number: int) -> str:
    suffix = f"-{number}"
    return base[: BOT_HANDLE_MAX_LENGTH - len(suffix)].rstrip("-") + suffix


def handle_taken(conn: Connection, handle: str, *, agent_id: Optional[str] = None) -> bool:
    """Whether ``handle`` already names something other than ``agent_id``'s Bot.

    A handle is a Session id, a directory, and an Agent name at once, so any of them in use takes it: another Agent
    named so, another Agent's Bot record (removed ones too, since setting them again reconnects), a ``ses_<handle>``
    Session not run by ``agent_id``, or an existing directory, which is someone's memory. A Bot reconnecting to its
    own record's handle never asks.
    """
    owner_rows = conn.execute(
        select(agents.c.id).where(
            (agents.c.normalized_name == handle)
            | (func.json_extract(agents.c.metadata_json, f"$.{BOT_METADATA_KEY}.handle") == handle)
        )
    ).scalars().all()
    if any(owner != agent_id for owner in owner_rows):
        return True
    session_agent = conn.execute(
        select(agent_sessions.c.agent_id).where(agent_sessions.c.id == bot_session_id(handle)).limit(1)
    ).first()
    if session_agent is not None and (agent_id is None or session_agent[0] != agent_id):
        return True
    return bot_directory(handle).exists()


def free_handle(conn: Connection, base: str, *, agent_id: Optional[str] = None) -> str:
    """``base``, or ``base-2``, ``base-3``... skipping reserved handles and taken ones (D2 step 4)."""
    if not is_valid_handle(base):
        raise BotError("bot_handle_invalid", f"'{base}' is not a valid Bot handle", handle=base)
    candidate, number = base, 1
    while candidate in RESERVED_BOT_HANDLES or handle_taken(conn, candidate, agent_id=agent_id):
        number += 1
        candidate = _with_suffix(base, number)
    return candidate


def require_new_handle(conn: Connection, handle: str, *, agent_id: Optional[str] = None) -> str:
    """A handle a caller chose: valid, not reserved, and free. Never suffixed: the caller asked for this one."""
    if not is_valid_handle(handle):
        raise BotError(
            "bot_handle_invalid",
            f"'{handle}' is not a valid Bot handle: 2 to 24 lowercase letters, digits, or hyphens, starting with a "
            "letter and not ending with a hyphen",
            handle=handle,
        )
    if handle in RESERVED_BOT_HANDLES:
        raise BotError("bot_handle_reserved", f"Bot handle '{handle}' is reserved", handle=handle)
    if handle_taken(conn, handle, agent_id=agent_id):
        raise BotError("bot_handle_taken", f"Bot handle '{handle}' is already taken", status=409, handle=handle)
    return handle


def fallback_handle_base(display_name: str) -> str:
    """The handle base without a model: the normalized ASCII name, else the transliteration, else ``bot``."""
    return ascii_name_handle(display_name) or transliterated_handle(display_name) or "bot"


# --- the Bot ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bot:
    handle: str
    display_name: str
    avatar: Optional[dict[str, Any]]
    position: int
    agent: VibeAgent
    hide_details: bool = True

    @property
    def builtin(self) -> bool:
        return is_main_bot_agent(self.agent)

    @property
    def session_id(self) -> str:
        return bot_session_id(self.handle)

    @property
    def directory(self) -> Path:
        return bot_directory(self.handle)

    @property
    def role(self) -> str:
        return str(self.agent.description or "")

    def avatar_payload(self) -> Optional[dict[str, Any]]:
        if self.builtin:
            # Whatever an earlier version let the user store, the main Bot is the mascot.
            return {"kind": "mascot"}
        avatar = self.avatar or {}
        if avatar.get("kind") == "emoji":
            return {"kind": "emoji", "emoji": avatar.get("emoji"), "hue": avatar.get("hue")}
        if avatar.get("kind") == "image":
            image = self.directory / AVATAR_FILENAME
            try:
                version = int(image.stat().st_mtime_ns)
            except OSError:
                version = None
            if version is not None:
                return {"kind": "image", "url": f"/api/bots/{self.handle}/avatar?v={version}"}
        return None

    def to_payload(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            # Empty means the main Bot's localized default name (D5).
            "display_name": self.display_name,
            "avatar": self.avatar_payload(),
            "role": self.role,
            "agent_name": self.agent.name,
            "session_id": self.session_id,
            "position": self.position,
            "builtin": self.builtin,
            "hide_details": self.hide_details,
        }


def bot_from_agent(agent: VibeAgent) -> Optional[Bot]:
    """The Bot an Agent is, or None: archived, a removed role, or no Bot record."""
    if agent.archived_at is not None:
        return None
    record = agent_bot_record(agent)
    if record is None:
        return None
    try:
        position = int(record.get("position") or 0)
    except (TypeError, ValueError):
        position = 0
    avatar = record.get("avatar")
    return Bot(
        handle=str(record["handle"]),
        display_name=str(record.get("display_name") or ""),
        avatar=dict(avatar) if isinstance(avatar, dict) else None,
        position=0 if is_main_bot_agent(agent) else position,
        agent=agent,
        # Records written before the switch existed have no value: details stay hidden.
        hide_details=record.get("hide_details") is not False,
    )


def _display_order(bot: Bot) -> tuple[int, int, str]:
    return (0 if bot.builtin else 1, bot.position, bot.handle)


def _agent_rows(conn: Connection) -> list[VibeAgent]:
    rows = conn.execute(
        select(agents).where(agents.c.archived_at.is_(None)).where(
            func.json_extract(agents.c.metadata_json, f"$.{BOT_METADATA_KEY}.handle").is_not(None)
        )
    ).mappings().all()
    return [VibeAgentStore._from_row(row) for row in rows]


def list_bots_in(conn: Connection) -> list[Bot]:
    """Every Bot in display order: the main Bot first, then by position."""
    bots = [bot for bot in map(bot_from_agent, _agent_rows(conn)) if bot is not None]
    return sorted(bots, key=_display_order)


def hidden_bot_session_ids(conn: Connection, context: AuthorizationContext | None) -> list[str]:
    """Bots' Sessions a reader may not see: every one for a non-Owner (D1), else those of removed Bots."""
    owner = context is None or context.is_instance_owner
    return sorted(stored_hidden_bot_session_ids(conn, owner=owner))


# --- directories ------------------------------------------------------------------------------------------------


def ensure_bot_directory(handle: str) -> Path:
    """The Bot's working directory: created with ``git init`` and nothing else (D7). Idempotent."""
    directory = bot_directory(handle)
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / ".git").exists():
        init_bot_repository(directory)
    return directory


def init_bot_repository(directory: Path) -> bool:
    from core.bot_git import bot_git_environment
    from core.git_binary import resolve_git

    git = resolve_git()
    if git is None:
        logger.warning("Git is unavailable; the Bot directory %s starts without history", directory)
        return False
    try:
        result = subprocess.run(
            [str(git.path), "-c", "init.defaultBranch=main", "init", "--quiet", str(directory)],
            check=False,
            capture_output=True,
            text=True,
            env=bot_git_environment(),
            timeout=GIT_INIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("git init failed for the Bot directory %s", directory, exc_info=True)
        return False
    if result.returncode != 0:
        logger.warning("git init failed for the Bot directory %s: %s", directory, result.stderr.strip())
        return False
    return True


# --- validation -------------------------------------------------------------------------------------------------


def _clean_display_name(value: Any, *, required: bool) -> str:
    name = " ".join(str(value or "").split())
    if not name and required:
        raise BotError("bot_display_name_required", "A Bot needs a display name")
    if len(name) > DISPLAY_NAME_MAX_LENGTH:
        raise BotError(
            "bot_display_name_too_long", f"A Bot's display name has at most {DISPLAY_NAME_MAX_LENGTH} characters"
        )
    return name


def clean_emoji_avatar(value: Any) -> Optional[dict[str, Any]]:
    """An emoji avatar (``{"emoji": ..., "hue": 0-359}``), or None to clear it."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BotError("bot_avatar_invalid", "An avatar is an object with an emoji and a hue")
    emoji = str(value.get("emoji") or "").strip()
    hue = value.get("hue")
    if (
        not emoji
        or len(emoji) > EMOJI_MAX_LENGTH
        or any(char.isspace() or ord(char) < 32 for char in emoji)
        or isinstance(hue, bool)
        or not isinstance(hue, int)
        or not 0 <= hue < 360
    ):
        raise BotError("bot_avatar_invalid", "An emoji avatar needs one emoji and a hue from 0 to 359")
    return {"kind": "emoji", "emoji": emoji, "hue": hue}


def scaled_avatar_png(data: bytes) -> bytes:
    """An uploaded image as a ``AVATAR_SIZE`` square PNG, center-cropped (D5)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise BotError("bot_avatar_invalid", "The avatar upload is empty")
    if len(data) > AVATAR_MAX_UPLOAD_BYTES:
        raise BotError("bot_avatar_too_large", "The avatar upload is larger than 10 MB", status=413)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGBA")
            image = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise BotError("bot_avatar_invalid", "The avatar upload is not a readable image") from exc


# --- the service ------------------------------------------------------------------------------------------------


class BotService:
    """Bot operations over the Agent catalog, Sessions, and directories. Owner only (D1)."""

    def __init__(self, store: Optional[VibeAgentStore] = None) -> None:
        self._own_store = store is None
        self.store = store or VibeAgentStore()
        self.engine = self.store.engine

    def close(self) -> None:
        if self._own_store:
            self.store.close()

    def __enter__(self) -> "BotService":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # reads

    def list_bots(self, *, authorization_context: Any = None) -> list[Bot]:
        require_instance_role(authorization_context, "owner")
        with self.engine.connect() as conn:
            return list_bots_in(conn)

    def get_bot(self, handle: str, *, authorization_context: Any = None) -> Bot:
        require_instance_role(authorization_context, "owner")
        with self.engine.connect() as conn:
            return self._require_bot(conn, handle)

    @staticmethod
    def _require_bot(conn: Connection, handle: str) -> Bot:
        handle = str(handle or "").strip()
        for bot in list_bots_in(conn):
            if bot.handle == handle:
                return bot
        raise _not_found(handle)

    # the Bot's Session and directory

    def ensure_home(self, bot: Bot, *, authorization_context: Any = None) -> dict[str, Any]:
        """Prepare the Bot's directory and Session, creating what is missing; returns the Session payload."""
        from storage import workbench_sessions_service

        context = require_instance_role(authorization_context, "owner")
        directory = ensure_bot_directory(bot.handle)
        with self.engine.begin() as conn:
            return workbench_sessions_service.ensure_bot_session(
                conn,
                session_id=bot.session_id,
                agent={"id": bot.agent.id, "name": bot.agent.name},
                workdir=str(directory),
                title=bot.display_name or None,
                authorization_context=context,
            )

    def ensure_main_bot(self, *, authorization_context: Any = None) -> Bot:
        """The main Bot with its Agent, directory, and ``ses_main``, created or repaired on use."""
        require_instance_role(authorization_context, "owner")
        bot = bot_from_agent(self.store.ensure_main_bot_agent())
        if bot is None:
            raise BotError("bot_main_unavailable", "The main Bot's Agent is unavailable", status=409)
        self.ensure_home(bot, authorization_context=authorization_context)
        return bot

    # writes

    def create_bot(
        self,
        *,
        display_name: Any,
        handle: Optional[str] = None,
        role: Any = None,
        avatar: Any = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        authorization_context: Any = None,
    ) -> Bot:
        """A new Vibey Agent and its Bot (D4 ``create``): its directory and Session are created with it.

        The Agent is named after the handle. Its role is its description (D1). Without a model it follows the
        main Bot's model and effort (D5).
        """
        context = require_instance_role(authorization_context, "owner")
        name = _clean_display_name(display_name, required=True)
        avatar_record = clean_emoji_avatar(avatar)
        if not str(model or "").strip():
            # The main Bot's model, or Vibey's while the main Bot has none yet.
            for follows in (self.store.get_main_bot_agent(), self.store.get_builtin_default_agent_for_backend("vibey")):
                if follows is not None and str(follows.model or "").strip():
                    model, reasoning_effort = follows.model, reasoning_effort or follows.reasoning_effort
                    break
        requested = str(handle or "").strip() or None
        chosen: dict[str, str] = {}

        def claim(conn: Connection, agent: VibeAgent) -> None:
            # Decided under the writer lock, in the inserting transaction, so two creates cannot take one handle.
            if requested is not None:
                resolved = require_new_handle(conn, requested)
            else:
                resolved = free_handle(conn, fallback_handle_base(name))
            if resolved != agent.normalized_name:
                raise _HandleMoved(resolved)
            chosen["handle"] = resolved

        base = requested or fallback_handle_base(name)
        for _attempt in range(5):
            with self.engine.connect() as conn:
                candidate = require_new_handle(conn, base) if requested else free_handle(conn, base)
            try:
                agent = self.store._create(
                    name=candidate,
                    backend=BOT_BACKEND,
                    description=str(role or "").strip() or None,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    store_metadata={
                        BOT_METADATA_KEY: {
                            "handle": candidate,
                            "display_name": name,
                            "avatar": avatar_record,
                            "position": self._next_position(),
                        }
                    },
                    user_context=context,
                    before_insert=claim,
                )
            except _HandleMoved:
                continue
            break
        else:
            raise BotError("bot_handle_taken", "Could not reserve a Bot handle; try again", status=409)
        bot = bot_from_agent(agent)
        assert bot is not None
        self.ensure_home(bot, authorization_context=context)
        publish_bots_changed(bot.handle)
        return bot

    def set_bot(
        self,
        agent_name: str,
        *,
        display_name: Any,
        avatar: Any = None,
        authorization_context: Any = None,
    ) -> Bot:
        """Make an existing enabled Vibey Agent a Bot (D4 ``set``).

        Its handle is its Agent name, or the handle it had if it was a Bot before, which reconnects its Session and
        directory (D1).
        """
        context = require_instance_role(authorization_context, "owner")
        name = _clean_display_name(display_name, required=True)
        avatar_record = clean_emoji_avatar(avatar)
        try:
            normalized = normalize_agent_name(agent_name)
        except ValueError as exc:
            raise BotError("bot_agent_not_found", f"Agent '{agent_name}' not found", status=404) from exc
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(select(agents).where(agents.c.normalized_name == normalized).limit(1)).mappings().first()
            if row is None or row["archived_at"] is not None:
                raise BotError("bot_agent_not_found", f"Agent '{agent_name}' not found", status=404)
            agent = VibeAgentStore._from_row(row)
            VibeAgentStore._require_manageable_agent(conn, agent, user_context=context)
            if agent.backend != BOT_BACKEND:
                raise BotError(
                    "bot_agent_backend", f"Only Vibey Agents can be Bots; '{agent.name}' runs on {agent.backend}"
                )
            if is_builtin_default_agent(agent):
                # Vibey stays an ordinary worker: listed among Agents, mentionable, and the default (bots.md D9).
                raise BotError("bot_agent_builtin", f"The built-in Agent '{agent.name}' cannot be a Bot", status=409)
            if _is_default_agent(conn, agent):
                raise BotError(
                    "bot_agent_default", f"Agent '{agent.name}' is the default Agent; choose another default first",
                    status=409,
                )
            if not agent.enabled:
                raise BotError("bot_agent_disabled", f"Agent '{agent.name}' is disabled", status=409)
            if agent_bot_record(agent) is not None:
                raise BotError("bot_agent_is_bot", f"Agent '{agent.name}' is already a Bot", status=409)
            previous = agent.metadata.get(BOT_METADATA_KEY)
            previous = dict(previous) if isinstance(previous, dict) else {}
            handle = str(previous.get("handle") or "")
            if not is_valid_handle(handle):
                handle = free_handle(conn, normalize_handle(agent.normalized_name) or fallback_handle_base(name),
                                     agent_id=agent.id)
            record = {
                "handle": handle,
                "display_name": name,
                "avatar": avatar_record if avatar is not None else previous.get("avatar"),
                "position": self._next_position(conn),
            }
            # Setting a removed Bot again reconnects its conversation as the user left it.
            if isinstance(previous.get("hide_details"), bool):
                record["hide_details"] = previous["hide_details"]
            self._write_record(conn, agent, record)
            agent = self._reload(conn, agent.id)
            self._sync_session_title(conn, bot_session_id(handle), name)
        bot = bot_from_agent(agent)
        assert bot is not None
        self.ensure_home(bot, authorization_context=context)
        publish_bots_changed(bot.handle)
        return bot

    def update_bot(
        self,
        handle: str,
        *,
        display_name: Any = ...,
        avatar: Any = ...,
        position: Any = ...,
        hide_details: Any = ...,
        authorization_context: Any = None,
    ) -> Bot:
        """Change a Bot's display name, emoji avatar, position among the other Bots, or Hide details (D5 ``PATCH``)."""
        context = require_instance_role(authorization_context, "owner")
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            bot = self._require_bot(conn, handle)
            VibeAgentStore._require_manageable_agent(conn, bot.agent, user_context=context)
            if avatar is not ... and bot.builtin:
                raise _main_avatar_locked()
            record = dict(agent_bot_record(bot.agent) or {})
            if display_name is not ...:
                record["display_name"] = _clean_display_name(display_name, required=not bot.builtin)
            if avatar is not ...:
                record["avatar"] = clean_emoji_avatar(avatar)
            if hide_details is not ...:
                if not isinstance(hide_details, bool):
                    raise BotError("bot_hide_details_invalid", "Hide details is on or off")
                record["hide_details"] = hide_details
            self._write_record(conn, bot.agent, record)
            if position is not ...:
                if bot.builtin:
                    raise BotError("bot_main_position", "The main Bot is always first")
                if isinstance(position, bool) or not isinstance(position, int) or position < 1:
                    raise BotError("bot_position_invalid", "A Bot's position is a whole number from 1")
                self._move(conn, bot.agent.id, position)
            if display_name is not ...:
                self._sync_session_title(conn, bot.session_id, record["display_name"])
            updated = self._require_bot(conn, handle)
        publish_bots_changed(handle)
        return updated

    def remove_bot(self, handle: str, *, authorization_context: Any = None) -> Bot:
        """Drop the Bot role, keeping the Agent, the Session, and the directory (D4 ``remove``)."""
        context = require_instance_role(authorization_context, "owner")
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            bot = self._require_bot(conn, handle)
            if bot.builtin:
                raise BotError("bot_main_not_removable", "The main Bot cannot be removed", status=409)
            VibeAgentStore._require_manageable_agent(conn, bot.agent, user_context=context)
            record = dict(agent_bot_record(bot.agent) or {})
            record["removed_at"] = _now()
            self._write_record(conn, bot.agent, record)
            self._compact_positions(conn)
        publish_bots_changed(handle)
        return bot

    def set_avatar_image(self, handle: str, data: bytes, *, authorization_context: Any = None) -> Bot:
        """Store an uploaded avatar as ``avatar.png`` in the Bot's directory, scaled to 256px (D5)."""
        bot = self.get_bot(handle, authorization_context=authorization_context)
        if bot.builtin:
            # Refused before anything is written: the main Bot's directory gets no avatar it would never show.
            raise _main_avatar_locked()
        png = scaled_avatar_png(data)
        directory = ensure_bot_directory(bot.handle)
        target = directory / AVATAR_FILENAME
        temporary = directory / f".{AVATAR_FILENAME}.tmp"
        temporary.write_bytes(png)
        temporary.replace(target)
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            bot = self._require_bot(conn, handle)
            record = dict(agent_bot_record(bot.agent) or {})
            record["avatar"] = {"kind": "image"}
            self._write_record(conn, bot.agent, record)
            updated = self._require_bot(conn, handle)
        publish_bots_changed(handle)
        return updated

    def avatar_path(self, handle: str, *, authorization_context: Any = None) -> Path:
        bot = self.get_bot(handle, authorization_context=authorization_context)
        path = bot.directory / AVATAR_FILENAME
        if bot.builtin or (bot.avatar or {}).get("kind") != "image" or not path.is_file():
            raise BotError("bot_avatar_missing", f"Bot '{handle}' has no uploaded avatar", status=404)
        return path

    # internals

    @staticmethod
    def _reload(conn: Connection, agent_id: str) -> VibeAgent:
        return VibeAgentStore._from_row(conn.execute(select(agents).where(agents.c.id == agent_id)).mappings().one())

    @staticmethod
    def _write_record(conn: Connection, agent: VibeAgent, record: dict[str, Any]) -> None:
        current = conn.execute(select(agents.c.metadata_json).where(agents.c.id == agent.id)).scalar_one()
        try:
            metadata = json.loads(current or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata[BOT_METADATA_KEY] = record
        conn.execute(
            update(agents)
            .where(agents.c.id == agent.id)
            .values(metadata_json=json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), updated_at=_now())
        )

    def _next_position(self, conn: Optional[Connection] = None) -> int:
        if conn is None:
            with self.engine.connect() as fresh:
                return self._next_position(fresh)
        others = [bot.position for bot in list_bots_in(conn) if not bot.builtin]
        return max(others, default=0) + 1

    def _move(self, conn: Connection, agent_id: str, position: int) -> None:
        ordered = [bot for bot in list_bots_in(conn) if not bot.builtin]
        moving = next(bot for bot in ordered if bot.agent.id == agent_id)
        ordered.remove(moving)
        ordered.insert(min(position, len(ordered) + 1) - 1, moving)
        self._renumber(conn, ordered)

    def _compact_positions(self, conn: Connection) -> None:
        self._renumber(conn, [bot for bot in list_bots_in(conn) if not bot.builtin])

    def _renumber(self, conn: Connection, ordered: Iterable[Bot]) -> None:
        for index, bot in enumerate(ordered, start=1):
            if bot.position != index:
                record = dict(agent_bot_record(self._reload(conn, bot.agent.id)) or {})
                record["position"] = index
                self._write_record(conn, bot.agent, record)

    @staticmethod
    def _sync_session_title(conn: Connection, session_id: str, display_name: str) -> None:
        """A Bot's Session is titled by its display name; the main Bot's empty name clears it (the UI's default)."""
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == session_id)
            .values(
                title=display_name or None,
                updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )


def _is_default_agent(conn: Connection, agent: VibeAgent) -> bool:
    """Whether the instance's recorded default Agent is ``agent``: new chats start on it, so it cannot be a Bot."""
    name = VibeAgentStore._default_agent_name(conn)
    try:
        return bool(name) and normalize_agent_name(name) == agent.normalized_name
    except ValueError:
        return False


class _HandleMoved(Exception):
    """The handle checked before the insert was taken by the time the insert held the lock."""

    def __init__(self, handle: str) -> None:
        super().__init__(handle)
        self.handle = handle
