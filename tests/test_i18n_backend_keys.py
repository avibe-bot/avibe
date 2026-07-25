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

from core.run_settlement import SETTLEMENT_I18N_KEYS
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


@pytest.mark.parametrize("settled_by,key", sorted(SETTLEMENT_I18N_KEYS.items()))
def test_every_run_settlement_reason_resolves(settled_by: str, key: str) -> None:
    # These strings land in the run's user-visible ``error`` column, so an unresolved
    # key would show up verbatim in the Runs UI and the callback message.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (settled_by={settled_by})"
        assert resolved.strip()
