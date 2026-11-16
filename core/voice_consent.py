"""Each account's answer to "send voice to third-party AI services?", stored in ``state_meta``.

Voice input sends recordings (and some conversation context) through avibe.bot to
third-party speech and language model services. App Store guideline 5.1.2(i) asks for
explicit permission before that, so each account answers once on this Avibe. The key is
the same account key the home and notification preferences use: ``remote:<sub>`` for a
signed-in avibe.bot user, ``local`` for this machine's own Workbench.

The answer records the disclosure revision it was given against. When what is sent or
where it goes changes, raise ``VOICE_DISCLOSURE_REVISION`` and every account is asked again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from core.chat_discovery import get_state_meta, update_state_meta

CONSENT_KEY_PREFIX = "ai_consent:"
# The revision of the disclosure the voice question shows. An answer given to an older one
# no longer counts.
VOICE_DISCLOSURE_REVISION = 1

VoiceConsent = Literal["granted", "declined"]
VOICE_CONSENT_CHOICES: tuple[VoiceConsent, ...] = ("granted", "declined")


def consent_key(user_key: str) -> str:
    """Return the durable state key for one trusted account key."""

    if not isinstance(user_key, str) or not user_key.strip():
        raise ValueError("user_key must be a non-empty string")
    return f"{CONSENT_KEY_PREFIX}{user_key.strip()}"


def get_voice_consent(user_key: str, *, db_path: Path | None = None) -> VoiceConsent | None:
    """The account's current answer, or ``None`` when it has not answered this disclosure."""

    document = get_state_meta(consent_key(user_key), db_path=db_path)
    voice = document.get("voice") if isinstance(document, dict) else None
    if not isinstance(voice, dict):
        return None
    choice = voice.get("choice")
    revision = voice.get("revision")
    if choice not in VOICE_CONSENT_CHOICES or not isinstance(revision, int) or isinstance(revision, bool):
        return None
    if revision < VOICE_DISCLOSURE_REVISION:
        return None
    return choice


def voice_consent_granted(user_key: str, *, db_path: Path | None = None) -> bool:
    return get_voice_consent(user_key, db_path=db_path) == "granted"


def set_voice_consent(
    user_key: str,
    choice: Any,
    *,
    db_path: Path | None = None,
) -> VoiceConsent:
    """Record the account's answer to the current disclosure; other stored features are kept."""

    if choice not in VOICE_CONSENT_CHOICES:
        raise ValueError("choice must be 'granted' or 'declined'")

    def merge(raw: Any) -> dict[str, Any]:
        document = dict(raw) if isinstance(raw, dict) else {}
        document["voice"] = {
            "choice": choice,
            "revision": VOICE_DISCLOSURE_REVISION,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        return document

    update_state_meta(consent_key(user_key), merge, db_path=db_path)
    return choice
