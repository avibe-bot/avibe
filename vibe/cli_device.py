"""Lazy entry for the independently installed device package."""
from __future__ import annotations

import json


def main(arguments: list[str]) -> int:
    try:
        from avibe_device.cli import main as device_main
    except ImportError:
        from vibe.i18n import t

        print(json.dumps({"ok": False, "code": "device_plugin_missing", "error": t("device.pluginMissing", "en")}))
        return 1
    return device_main(arguments)
