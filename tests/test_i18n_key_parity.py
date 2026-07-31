"""Backend i18n bundles must declare the same key set in every language.

``vibe/i18n/__init__.py`` falls back to English for a missing key, so a
zh-only gap degrades silently: the user sees English mixed into an otherwise
translated message and nothing fails. Adding a key to one file only was
uncatchable before this test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

I18N_DIR = Path(__file__).resolve().parents[1] / "vibe" / "i18n"


def _flatten(payload: object, prefix: str = "") -> set[str]:
    if not isinstance(payload, dict):
        return {prefix}
    keys: set[str] = set()
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        keys |= _flatten(value, path)
    return keys


def _load(lang: str) -> dict:
    with (I18N_DIR / f"{lang}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def test_en_and_zh_declare_the_same_keys() -> None:
    en_keys = _flatten(_load("en"))
    zh_keys = _flatten(_load("zh"))

    assert sorted(en_keys - zh_keys) == [], "keys missing from zh.json"
    assert sorted(zh_keys - en_keys) == [], "keys missing from en.json"


def test_every_shipped_language_matches_the_english_key_set() -> None:
    """Guards the same rule for any language file added later."""
    en_keys = _flatten(_load("en"))
    languages = sorted(path.stem for path in I18N_DIR.glob("*.json"))

    assert "en" in languages and "zh" in languages
    for lang in languages:
        assert _flatten(_load(lang)) == en_keys, f"{lang}.json key set diverged"
