from __future__ import annotations

import stat
import tomllib
from pathlib import Path

from memory_poc.generated_config import write_generated_config


def test_generated_config_is_chat_only_and_has_no_endpoint_or_key(tmp_path: Path) -> None:
    generated = write_generated_config(everos_root=tmp_path / "everos-root", timezone="Asia/Shanghai")

    provider = tomllib.loads(generated.everos_toml.read_text(encoding="utf-8"))
    ome = tomllib.loads(generated.ome_toml.read_text(encoding="utf-8"))
    rendered = generated.everos_toml.read_text(encoding="utf-8")

    assert provider["memorize"]["mode"] == "chat"
    assert provider["memory"]["timezone"] == "Asia/Shanghai"
    assert provider["rerank"] == {"model": "", "base_url": ""}
    assert ome["strategies"]["reflect_episodes"]["enabled"] is False
    assert ome["strategies"]["extract_foresight"]["enabled"] is False
    assert "https://" not in rendered
    assert "api_key" not in rendered
    assert stat.S_IMODE(generated.everos_toml.stat().st_mode) == 0o600
