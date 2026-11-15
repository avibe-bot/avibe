"""Static checks for the approved iOS Local Network permission copy."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

ROOT = Path(__file__).parents[1]
INFO_PLIST = ROOT / "ios/Avibe/Resources/Info.plist"
INFO_PLIST_STRINGS = ROOT / "ios/Avibe/Resources/InfoPlist.xcstrings"
KEY = "NSLocalNetworkUsageDescription"

APPROVED_EN = (
    "Lets Avibe connect directly to the computer running Avibe when it's on the same network. "
    "If you don't allow it, Avibe still works over the internet."
)
APPROVED_ZH_HANS = "允许 Avibe 在同一网络中直接连接运行 Avibe 的电脑。不允许也能正常使用，只是经由互联网连接。"


def _catalog_entry() -> dict:
    catalog = json.loads(INFO_PLIST_STRINGS.read_text(encoding="utf-8"))
    assert catalog["sourceLanguage"] == "en"
    return catalog["strings"][KEY]


def test_info_plist_base_copy_is_the_approved_english() -> None:
    info = plistlib.loads(INFO_PLIST.read_bytes())

    assert info[KEY] == APPROVED_EN


def test_string_catalog_has_the_approved_copy_in_both_locales() -> None:
    localizations = _catalog_entry()["localizations"]

    assert set(localizations) == {"en", "zh-Hans"}
    assert localizations["en"]["stringUnit"] == {"state": "translated", "value": APPROVED_EN}
    assert localizations["zh-Hans"]["stringUnit"] == {"state": "translated", "value": APPROVED_ZH_HANS}


def test_copy_uses_plain_apostrophes_and_names_no_transport_internals() -> None:
    localizations = _catalog_entry()["localizations"]
    values = [localizations[locale]["stringUnit"]["value"] for locale in ("en", "zh-Hans")]

    assert "\u2019" not in APPROVED_EN
    for value in values:
        lowered = value.lower()
        for internal in ("cloudflare", "tunnel", "iroh", "ip address", "udp"):
            assert internal not in lowered
