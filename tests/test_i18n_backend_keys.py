"""Key-parity guard for the backend i18n bundles.

``vibe/i18n`` falls back to English for a missing key, so a translation added to
one bundle and forgotten in the other degrades silently — the user sees English in
a Chinese conversation, and a typo'd key leaks the raw dotted path into a message.
Both failure modes are cheap to catch here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.failure_notices import (
    NOTICE_FAILURE_CLASS_I18N_KEYS,
    NOTICE_REASON_I18N_KEYS,
    NOTICE_REASON_UNKNOWN_I18N_KEY,
    PER_FIRE_INTERRUPT_REASONS,
)
from core.run_settlement import (
    RUN_INTERRUPTION_REASONS,
    SETTLEMENT_I18N_KEYS,
    SWEEP_I18N_KEYS,
)
from core.services.sessions import SESSION_ARCHIVED_I18N_KEY, session_archived_message
from core.show_session_events import SHOW_EVENT_ERROR_I18N_KEYS
from storage.background import (
    SWEEP_REASON_ORPHANED,
    SWEEP_REASON_QUEUE_HOLD_EXPIRED,
    SWEEP_REASON_TRANSPORT_UNAVAILABLE,
)
from vibe.i18n import get_supported_languages, t

I18N_DIR = Path(__file__).resolve().parents[1] / "vibe" / "i18n"


def _flatten(value: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flat.update(_flatten(item, path))
        else:
            flat[path] = item
    return flat


def _bundle(lang: str) -> dict[str, str]:
    return _flatten(json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8")))


def test_backend_bundles_have_identical_keys() -> None:
    en = _bundle("en")
    zh = _bundle("zh")
    assert sorted(en) == sorted(zh), (
        f"missing in zh: {sorted(set(en) - set(zh))}; missing in en: {sorted(set(zh) - set(en))}"
    )


def test_no_backend_translation_is_blank() -> None:
    for lang in ("en", "zh"):
        blank = [key for key, value in _bundle(lang).items() if not str(value).strip()]
        assert blank == [], f"{lang} has blank translations: {blank}"


@pytest.mark.parametrize(
    "reason,key",
    sorted(SETTLEMENT_I18N_KEYS.items()) + sorted(SWEEP_I18N_KEYS.items()),
)
def test_every_run_settlement_reason_resolves(reason: str, key: str) -> None:
    # These strings land in the run's user-visible ``error`` column, so an unresolved
    # key would show up verbatim in the Runs UI and the callback message.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (reason={reason})"
        assert resolved.strip()


def test_sweep_reason_i18n_map_covers_every_store_sweep_reason() -> None:
    # ``SWEEP_I18N_KEYS`` spells its keys as literals so ``core.run_settlement``
    # stays dependency-free (see the comment there). This is the guard that makes
    # that safe: add a sweep reason in the store without a translation and the
    # sweep would stamp a run with an empty ``error``, which reads to the user as
    # "it just failed" with no explanation.
    assert set(SWEEP_I18N_KEYS) == {
        SWEEP_REASON_ORPHANED,
        SWEEP_REASON_TRANSPORT_UNAVAILABLE,
        SWEEP_REASON_QUEUE_HOLD_EXPIRED,
    }


@pytest.mark.parametrize(
    "code,key",
    sorted(SHOW_EVENT_ERROR_I18N_KEYS.items()),
)
def test_every_show_event_error_resolves(code: str, key: str) -> None:
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (code={code})"
        assert resolved.strip()


def test_session_archived_message_resolves_in_every_language() -> None:
    # This string ships in the ``409 session_archived`` response body, which direct
    # API/CLI consumers read verbatim and the Web UI renders as its fallback when
    # ``errors.session_archived`` is missing — so an unresolved key would leak the
    # dotted path to a user, and a missing translation would leak English.
    for lang in get_supported_languages():
        resolved = session_archived_message(lang)
        assert resolved != SESSION_ARCHIVED_I18N_KEY, f"{SESSION_ARCHIVED_I18N_KEY} is not translated in {lang}"
        assert resolved.strip()
        assert resolved == t(SESSION_ARCHIVED_I18N_KEY, lang)


def test_notice_reason_i18n_map_covers_exactly_the_interruption_lane() -> None:
    # ``harness.notice.interrupted`` renders the reason INTO the sentence a user
    # reads, so an unmapped reason is a wire identifier leaking into product copy —
    # "was interrupted (backend_refresh)". Same drift guard as ``SWEEP_I18N_KEYS``
    # above: the map has to track ``RUN_INTERRUPTION_REASONS`` exactly, since that
    # frozenset is what gates the interrupted branch. A reason added there without a
    # label would silently fall back to the generic string and lose its explanation.
    assert set(NOTICE_REASON_I18N_KEYS) == set(RUN_INTERRUPTION_REASONS), (
        "unlabelled: "
        f"{sorted(set(RUN_INTERRUPTION_REASONS) - set(NOTICE_REASON_I18N_KEYS))}; "
        "stale: "
        f"{sorted(set(NOTICE_REASON_I18N_KEYS) - set(RUN_INTERRUPTION_REASONS))}"
    )


@pytest.mark.parametrize(
    "reason,key",
    sorted(NOTICE_REASON_I18N_KEYS.items()) + [("<unmapped>", NOTICE_REASON_UNKNOWN_I18N_KEY)],
)
def test_every_notice_reason_label_resolves(reason: str, key: str) -> None:
    # Including the fallback: an unknown reason must render a LOCALIZED generic
    # label, so if that key were missing the notice would print the dotted path
    # instead — a worse leak than the raw reason it replaced.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (reason={reason})"
        assert resolved.strip()


def test_notice_failure_class_map_covers_exactly_the_per_fire_lane() -> None:
    # The FAILED lane's own vocabulary, drift-pinned the same way the interrupted
    # lane's is — and against a DERIVED set, so the two maps cannot both claim a
    # reason or both drop one. ``PER_FIRE_INTERRUPT_REASONS`` is the settlement and
    # sweep vocabularies minus ``RUN_INTERRUPTION_REASONS``, i.e. exactly the
    # discriminator ``is_interruption`` applies, so a reason moved between lanes
    # fails here instead of silently losing its label.
    assert set(NOTICE_FAILURE_CLASS_I18N_KEYS) == set(PER_FIRE_INTERRUPT_REASONS), (
        "unlabelled: "
        f"{sorted(set(PER_FIRE_INTERRUPT_REASONS) - set(NOTICE_FAILURE_CLASS_I18N_KEYS))}; "
        "stale: "
        f"{sorted(set(NOTICE_FAILURE_CLASS_I18N_KEYS) - set(PER_FIRE_INTERRUPT_REASONS))}"
    )
    # And the two maps are disjoint: one reason, one lane, one label.
    assert not set(NOTICE_FAILURE_CLASS_I18N_KEYS) & set(NOTICE_REASON_I18N_KEYS)


@pytest.mark.parametrize("reason,key", sorted(NOTICE_FAILURE_CLASS_I18N_KEYS.items()))
def test_every_notice_failure_class_label_resolves(reason: str, key: str) -> None:
    # No generic fallback here by design (``notice_failure_class_i18n_key`` returns
    # ``None`` and the line is omitted), so every mapped label has to be real in
    # every language or the class line prints a dotted path.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (reason={reason})"
        assert resolved.strip()
        assert reason not in resolved, (
            f"{key} leaks the wire value {reason!r} into product copy in {lang}"
        )
