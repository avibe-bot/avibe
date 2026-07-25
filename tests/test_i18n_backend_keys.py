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

from core.run_settlement import SETTLEMENT_I18N_KEYS, SWEEP_I18N_KEYS
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
