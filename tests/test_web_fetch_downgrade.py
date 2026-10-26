"""WF-1 release-order proof: rc4 ordinary content is portable, consent/attribution are not.

The frozen fragments are exact rc4 code at the owning persisted-read boundaries.
Existing search downgrades do not cover a new record variant or a shared service
identity used by two tools. All rewrites go to the test-owned directory.
"""
from __future__ import annotations

import json
import runpy
from datetime import datetime, timezone
from pathlib import Path

from config.v2_config import ModelHubConfig
from core.agent_core.messages import message_from_dict, message_to_dict
from core.handlers.model_hub import usage

FIXTURES = Path(__file__).parent / "fixtures/model_hub"
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def released(boundary):
    return runpy.run_path(str(FIXTURES / f"released_rc4_fetch_{boundary}.py"))


def test_rc4_reads_complete_ordinary_fetch_content_and_drops_only_unknown_metadata():
    raw = json.loads((Path(__file__).parent / "agent_core/fixtures/web_fetch_v21.json").read_text())
    old_message = released("messages")["message_from_dict"](raw)
    assert old_message.hosted is None
    old_wire = message_to_dict(old_message)
    assert old_wire == {key: value for key, value in raw.items() if key != "hosted"}
    assert message_to_dict(message_from_dict(old_wire)) == old_wire
    # In contrast, the WF-1 reader retains the structured metadata, not just text.
    assert message_to_dict(message_from_dict(raw)) == raw


def test_rc4_config_rewrite_loses_fetch_opt_outs_but_wf1_preserves_them():
    raw = json.loads((FIXTURES / "web_fetch_v21.json").read_text())
    config = ModelHubConfig.from_payload({"hosted_tools": raw["hosted_tools"]})
    assert config.to_payload()["hosted_tools"] == raw["hosted_tools"]
    old_loaded = released("config")["_hosted_tools_from_payload"](config.to_payload(), [])
    assert "web_fetch" not in old_loaded
    written = {"hosted_tools": {tool: value.to_payload() for tool, value in old_loaded.items()}}
    assert "web_fetch" not in ModelHubConfig.from_payload(written).hosted_tools


def test_rc4_ledger_rewrite_merges_exa_tools_and_reupgrade_attributes_to_search(tmp_path, monkeypatch):
    raw = json.loads((FIXTURES / "web_fetch_v21.json").read_text())
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(raw["usage"]))
    old = released("usage")
    # Exercise the real merge/write owner with the released row projector and key.
    # No mock implements loss: these exact rc4 functions discard tool and merge ids.
    with monkeypatch.context() as patch:
        for name in ("_normalize_service_row", "_row_key"):
            patch.setattr(usage, name, old[name])
        usage.BoundedUsageLedger(path, now=lambda: NOW).record_many([
            usage.ServiceUsageCall("src_exakeyless", "exa/mcp", NOW),
        ])
    rewritten = json.loads(path.read_text())
    assert len(rewritten) == 1 and rewritten[0]["requests"] == 6
    assert "tool" not in rewritten[0]
    read = usage.BoundedUsageLedger(path, now=lambda: NOW).report(window="7d", now=NOW)
    assert [(row.get("tool", "web_search"), row["requests"]) for row in read["services"]] == [("web_search", 6)]
