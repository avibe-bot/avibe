"""WF-2V: shared Web/IM projection facts, not hand-built UI cards.

The WF-1 reader tests miss localized synthetic labels, policy disclosure after
remote submission, and exact character/cost summaries. These fixtures also drive
the browser screenshots; expected phrases are asserted separately below.
"""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from core.agent_core.messages import HostedAttempt, HostedCost, HostedResult, ProxyExecution, ServiceExecutorOrigin, ToolCallBlock, WebFetchRecord
from core.handlers.model_hub.fetch_readers import fetch_status
from core.hosted_activity import hosted_line, proxy_activity
from tests.test_model_hub_web_fetch_readers import fetch_config

ROOT = Path(__file__).resolve().parents[1]


def activity_fixtures():
    def attempt(sid, label, kind="service", outcome="completed", code=None, usd=0):
        return HostedAttempt(sid, label, kind, outcome, error_code=code, cost=HostedCost("list", usd))
    local = attempt("src_localfetch", "Local", "local")
    exa = attempt("src_exakeyless", "Exa (free)")
    jina = attempt("src_jinakeyless", "Jina Reader (free)")
    base = WebFetchRecord(status="completed", requested_url="https://example.com/pricing", final_url="https://example.com/pricing/",
        title="Pricing — Example", content_type="text/markdown", source_content_type="text/html", content="正文。" * 1000,
        size_bytes=9000, source_size_bytes=182000, retrieved_at="2026-10-25T05:00:00Z", attempts=(local,))
    records = {
        "local_truncated": replace(base, truncated=True, truncation_reason="budget"),
        "jina_fallback": replace(base, requested_url="https://docs.example.com/guide", final_url=None,
            attempts=(replace(exa, outcome="failed", error_code="service_rate_limited"), jina)),
        "tavily": replace(base, requested_url="https://example.com/blog/launch", final_url=None,
            attempts=(attempt("src_tavily001", "Tavily", usd=0.0016),)),
        "exa": replace(base, requested_url="https://example.com/docs/api", attempts=(exa,)),
        "challenge": replace(base, requested_url="https://shop.example.com/item/42",
            attempts=(replace(local, outcome="failed", error_code="fetch_challenge"), exa)),
        "policy": replace(base, status="failed", requested_url="http://10.0.0.5/admin", final_url=None, content="", size_bytes=0,
            error="blocked address", error_code="fetch_url_blocked", attempts=()),
        "policy_after_service": replace(base, status="failed", final_url=None, content="", size_bytes=0,
            error="blocked address", error_code="fetch_url_blocked", attempts=(replace(exa, outcome="failed", error_code="fetch_target_unreadable"),)),
        "degraded": replace(base, transport_degraded=True, transport_degraded_reason="native load unavailable"),
    }
    result = {}
    for key, record in records.items():
        last = record.attempts[-1] if record.attempts else None
        origin = ServiceExecutorOrigin("local" if last.kind == "local" else "fixture", "local_fetch" if last.kind == "local" else "fetch_api", "fixture/fetch") if last else None
        execution = ProxyExecution(origin, last.source_id, "metered", cost=last.cost) if last else None
        card = proxy_activity(ToolCallBlock(key, "web_fetch", {"url": record.requested_url}), HostedResult(record, execution), duration_ms=1200)
        result[key] = {"activity": card.to_payload(), "en": hosted_line(card, "en"), "zh": hosted_line(card, "zh")}
    return result


def tools_fixture():
    config = fetch_config({})
    config.sources = [s for s in config.sources if s.vendor == "tavily"]
    success = {"source_id": "src_localfetch", "model_id": "local/fetch", "mode": "service", "at": "2026-10-25T05:00:00Z"}
    return fetch_status(config, status={"latest_ok": success, "verified": [success], "refused": []}, cooldown_until=lambda *_: None)


@pytest.mark.parametrize("name,expected", [
    ("local_truncated", ["🌐 读取网页 example.com", "本机", "3000 字", "已截断", "免费"]),
    ("jina_fallback", ["Exa（免费）", "限流", "Jina Reader（免费）", "免费"]),
    ("tavily", ["Tavily", "$0.0016"]),
    ("policy", ["本机策略拒绝", "未转发第三方"]),
    ("policy_after_service", ["本机策略拒绝", "Exa（免费）"]),
    ("challenge", ["本机", "浏览器验证", "Exa（免费）"]),
])
def test_fetch_lines_are_factual_localized_and_never_quote_page_text(name, expected):
    line = activity_fixtures()[name]["zh"]
    assert all(phrase in line for phrase in expected), line
    assert "正文" not in line and "Pricing" not in line
    if name == "policy_after_service":
        assert "未转发第三方" not in line


@pytest.mark.parametrize("lang,executor", [("en", "Exa (free)"), ("zh", "Exa（免费）")])
def test_fetch_im_names_the_executor_without_a_via_prefix(lang, executor):
    assert f" · {executor} · " in activity_fixtures()["exa"][lang]


@pytest.mark.parametrize("skipped", ["disabled", "cooling", "unavailable", "service_busy"])
def test_policy_disclosure_does_not_count_no_send_service_skips(skipped):
    record = WebFetchRecord(status="failed", requested_url="http://10.0.0.5/admin", content="", size_bytes=0,
        retrieved_at="2026-10-25T05:00:00Z", error="blocked address", error_code="fetch_url_blocked",
        attempts=(HostedAttempt("src_exakeyless", "Exa (free)", "service", "skipped", skipped=skipped),))
    card = proxy_activity(ToolCallBlock("policy", "web_fetch", {"url": record.requested_url}), HostedResult(record))
    assert card.to_payload()["policy_sources"] == []
    assert "未转发第三方" in hosted_line(card, "zh")


def test_browser_fixtures_are_real_shared_projections():
    assert json.loads((ROOT / "ui/src/lib/hostedFetchActivity.fixture.json").read_text()) == activity_fixtures()
    assert json.loads((ROOT / "ui/src/components/settings/models/webFetch.fixture.json").read_text()) == tools_fixture()
