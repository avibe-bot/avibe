"""Subscription quota: vendor report parsers, the service cache, and the adapter call."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from core.handlers.model_hub.quota import (
    QUOTA_FORCED_REFRESH_INTERVAL,
    QUOTA_REFRESH_INTERVAL,
    QuotaSourceRef,
    SubscriptionQuotaCache,
    SubscriptionQuotaError,
    parse_claude_quota,
    parse_codex_quota,
    parse_gemini_quota,
    parse_grok_quota,
)

CONTRACTS = Path("docs/plans/model-hub-contracts")
QUOTA_SCHEMA = json.loads((CONTRACTS / "quota-summary.schema.json").read_text(encoding="utf-8"))

# Shaped after what Claude Code 2.x reads from `/api/oauth/usage`.
CLAUDE_LEGACY = {
    "five_hour": {"utilization": 38.0, "resets_at": "2026-09-25T11:14:00.402+00:00"},
    "seven_day": {"utilization": 61, "resets_at": "2026-09-29T01:00:00+00:00"},
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 22.4, "resets_at": "2026-09-29T01:00:00+00:00"},
    "seven_day_overage_included": {"utilization": 86, "resets_at": "2026-09-29T01:00:00+00:00"},
    "extra_usage": {"is_enabled": True, "monthly_limit": 10000, "used_credits": 1240, "utilization": 12.4},
}
CLAUDE_LIMITS = {
    **CLAUDE_LEGACY,
    "limits": [
        {"kind": "session", "group": "session", "percent": 38, "resets_at": "2026-09-25T11:14:00Z",
         "scope": None, "severity": "normal", "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 61, "resets_at": "2026-09-29T01:00:00Z",
         "scope": None, "severity": "normal", "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 86, "resets_at": "2026-09-29T01:00:00Z",
         "scope": {"model": {"display_name": "Fable"}, "surface": None}, "severity": "warning", "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 5, "resets_at": "2026-09-29T01:00:00Z",
         "scope": {"model": {"display_name": "  俳句\n模型 "}, "surface": None}},
        {"kind": "monthly_surface", "group": "monthly", "percent": 40, "resets_at": None,
         "scope": {"model": None, "surface": {"display_name": "Claude 应用"}}},
        {"kind": "weekly_scoped", "percent": None, "scope": {"model": {"display_name": "Ghost"}}},
        "not-a-row",
    ],
}
# Shaped after openai/codex `RateLimitStatusPayload`.
CODEX_REPORT = {
    "plan_type": "pro",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 12, "limit_window_seconds": 18000,
                           "reset_after_seconds": 13200, "reset_at": 1790000000},
        "secondary_window": {"used_percent": 45, "limit_window_seconds": 604800,
                             "reset_after_seconds": 400000, "reset_at": 1790400000},
    },
    "additional_rate_limits": [
        {"limit_name": "GPT-5.2-Codex-Spark", "metered_feature": "codex_bengalfox",
         "rate_limit": {"primary_window": {"used_percent": 8, "limit_window_seconds": 604800,
                                           "reset_after_seconds": 400000, "reset_at": 1790400000},
                        "secondary_window": None}},
        {"limit_name": "实验模型", "metered_feature": "future_feature",
         "rate_limit": {"primary_window": {"used_percent": 3, "limit_window_seconds": 3600,
                                           "reset_after_seconds": 100, "reset_at": 1790000100}}},
        {"limit_name": "broken", "rate_limit": None},
    ],
    "credits": {"has_credits": False, "unlimited": False, "balance": None},
}
# Shaped after Antigravity's `v1internal:retrieveUserQuotaSummary` (groups of
# models sharing a 5-hour and a weekly bucket); every value is made up. Proto3
# omits a zero, so the spent 5-hour bucket carries only its reset.
GEMINI_SUMMARY = {
    "groups": [
        {"buckets": [
            {"bucketId": "gemini-weekly", "displayName": "Weekly Limit Remaining", "window": "weekly",
             "resetTime": "2026-09-29T15:52:48Z", "remainingFraction": 0.62},
            {"bucketId": "gemini-5h", "displayName": "Five Hour Limit Remaining", "window": "5h",
             "resetTime": "2026-09-25T05:10:00Z"},
        ], "displayName": "Gemini Models", "description": "Models within this group: Gemini Flash, Gemini Pro"},
        {"buckets": [
            {"bucketId": "3p-weekly", "window": "weekly", "resetTime": "2026-10-02T03:00:00Z", "remainingFraction": 1},
            {"bucketId": "3p-5h", "window": "5h", "resetTime": "2026-09-25T08:00:00Z", "remainingFraction": 0.9},
            {"bucketId": "3p-daily", "window": "daily", "remainingFraction": 0.5},
            {"bucketId": "3p-legacy", "window": "5h", "remainingFraction": 1, "disabled": True},
        ], "displayName": "Claude and GPT models"},
    ],
    "description": "Within each group, models share a weekly limit and a 5-hour limit.",
}
# Shaped after Grok's `/v1/billing?format=credits`; values are made up.
GROK_BILLING = {
    "config": {
        "creditUsagePercent": 37.5,
        "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-09-21T00:00:00Z",
                          "end": "2026-09-28T00:00:00Z"},
        "productUsage": [{"product": "grok-cli", "usagePercent": 30}],
    },
}


def _validate_windows(parsed):
    validator = Draft7Validator(QUOTA_SCHEMA["definitions"]["QuotaWindow"])
    for window in parsed["windows"]:
        validator.validate(window)


@pytest.mark.parametrize(
    ("state", "error_key", "valid"),
    [
        ("ok", None, True),
        ("ok", "models.quota.error.unavailable", False),
        *[(state, "models.quota.error.unavailable", True) for state in ("stale", "auth_expired", "unsupported", "error")],
        *[(state, None, False) for state in ("stale", "auth_expired", "unsupported", "error")],
    ],
)
def test_quota_contract_carries_a_reason_exactly_when_not_ok(state, error_key, valid):
    """A non-ok Source without a reason, or an ok Source with one, violates the contract."""

    source = {
        "source_id": "src_claude", "vendor": "anthropic", "display_name": "Claude Max", "account_label": None,
        "plan": None, "fetched_at": None, "state": state, "windows": [],
    }
    if error_key is not None:
        source["error_key"] = error_key
    errors = list(Draft7Validator(QUOTA_SCHEMA).iter_errors({"refresh_interval_seconds": 300, "sources": [source]}))
    assert (not errors) is valid


@pytest.mark.parametrize(
    ("kind", "scope_model", "valid"),
    [
        *[(kind, "Sonnet", True) for kind in ("model_session", "model_weekly")],
        *[(kind, None, False) for kind in ("model_session", "model_weekly")],
        *[(kind, None, True) for kind in ("session", "weekly", "monthly", "other")],
        *[(kind, "Sonnet", False) for kind in ("session", "weekly", "monthly", "other")],
        ("daily", None, False),
    ],
)
def test_quota_contract_names_a_model_exactly_for_model_weekly(kind, scope_model, valid):
    """A model-scoped window without its model, or a shared window with one, violates the contract."""

    window = {"id": "w", "kind": kind, "label": "Sonnet", "used_pct": 10, "window_seconds": None, "resets_at": None}
    if scope_model is not None:
        window["scope_model"] = scope_model
    errors = list(Draft7Validator(QUOTA_SCHEMA["definitions"]["QuotaWindow"]).iter_errors(window))
    assert (not errors) is valid


def test_claude_quota_parser_reads_legacy_windows_and_skips_nulls():
    """MH-QUOTA-001: Claude's fixed windows parse; null and app-only windows are skipped."""

    parsed = parse_claude_quota(json.dumps(CLAUDE_LEGACY))
    _validate_windows(parsed)
    by_id = {window["id"]: window for window in parsed["windows"]}
    assert list(by_id) == ["five_hour", "seven_day", "seven_day_sonnet", "seven_day_overage_included"]
    assert by_id["five_hour"]["kind"] == "session"
    assert by_id["five_hour"]["window_seconds"] == 18000
    assert by_id["five_hour"]["resets_at"] == "2026-09-25T11:14:00.402000Z"
    assert by_id["seven_day"]["kind"] == "weekly"
    assert by_id["seven_day_sonnet"] | {} == {
        "id": "seven_day_sonnet", "kind": "model_weekly", "label": "Sonnet", "scope_model": "Sonnet",
        "used_pct": 22.4, "window_seconds": 604800, "resets_at": "2026-09-29T01:00:00Z",
    }
    assert by_id["seven_day_overage_included"]["scope_model"] == "Fable"
    # Money stays out of this lane: extra usage is never a window or a field.
    assert "extra_usage" not in json.dumps(parsed)


def test_claude_quota_parser_prefers_server_limits_and_keeps_unknown_rows():
    """MH-QUOTA-002: Claude `limits[]` rows classify on kind, keep server model names (non-ASCII too), and render unknown kinds generically."""

    parsed = parse_claude_quota(json.dumps(CLAUDE_LIMITS, ensure_ascii=False).encode())
    _validate_windows(parsed)
    kinds = [(window["kind"], window["label"]) for window in parsed["windows"]]
    assert kinds == [
        ("session", "session"),
        ("weekly", "weekly"),
        ("model_weekly", "Fable"),
        ("model_weekly", "俳句 模型"),
        ("other", "Claude 应用"),
    ]
    other = parsed["windows"][-1]
    assert other["window_seconds"] is None and other["resets_at"] is None
    assert "scope_model" not in other


def test_quota_windows_mark_exactly_the_labels_that_are_upstream_keys():
    """MH-QUOTA-002/003: `label_is_key` marks a label taken from an upstream key, never a display name."""

    def flagged(windows):
        return {window["label"]: window.get("label_is_key", False) for window in windows}

    # Claude top level: an unknown key is its own label; known keys carry product copy.
    legacy = {**CLAUDE_LEGACY, "seven_day_cowork": {"utilization": 5, "resets_at": None}}
    windows = parse_claude_quota(json.dumps(legacy))["windows"]
    _validate_windows({"windows": windows})
    assert flagged(windows) == {"five_hour": False, "seven_day": False, "Sonnet": False, "Fable": False,
                                "seven_day_cowork": True}
    # Claude `limits[]`: only the `kind` fallback is a key; model and surface display names are text.
    limits = {"limits": [
        {"kind": "monthly_surface", "percent": 1, "scope": {"surface": {"display_name": "claude-code"}}},
        {"kind": "monthly_model", "percent": 2, "scope": {"model": {"display_name": "Claude_Code"}}},
        {"kind": "seven_day_cowork", "percent": 3, "scope": {}},
    ]}
    windows = parse_claude_quota(json.dumps(limits))["windows"]
    _validate_windows({"windows": windows})
    assert flagged(windows) == {"claude-code": False, "Claude_Code": False, "seven_day_cowork": True}
    # Codex: `limit_name` is display text; a lone `metered_feature` is the feature's id.
    window = {"used_percent": 3, "limit_window_seconds": 3600, "reset_after_seconds": 10, "reset_at": 1790000100}
    weekly = {**window, "limit_window_seconds": 604800}
    report = {"additional_rate_limits": [
        {"limit_name": "gpt_5_codex", "metered_feature": "x_feature", "rate_limit": {"primary_window": window}},
        {"metered_feature": "future_feature", "rate_limit": {"primary_window": window, "secondary_window": weekly}},
        {"metered_feature": "codex_bengalfox", "rate_limit": {"primary_window": weekly}},
    ]}
    windows = parse_codex_quota(json.dumps(report))["windows"]
    _validate_windows({"windows": windows})
    assert flagged(windows) == {"gpt_5_codex · 1h": False, "future_feature · 1h": True, "future_feature": True,
                                "Spark": False}
    assert next(w for w in windows if w["label"] == "future_feature")["scope_model"] == "future_feature"


@pytest.mark.parametrize(
    ("seconds", "label"),
    [(60, "1m"), (1800, "30m"), (3600, "1h"), (5400, "1h 30m"), (18000, "5h"), (86400, "1d"),
     (90000, "25h"), (91800, "25h 30m"), (1209600, "14d"), (89, "1m"), (5430, "1h 30m")],
)
def test_codex_generic_limit_names_its_window_length_as_written(seconds, label):
    """MH-QUOTA-003: An unrecognised Codex limit names its period exactly; a sub-hour window is not rounded to 1h."""

    report = {"additional_rate_limits": [{"limit_name": "实验模型", "rate_limit": {"primary_window": {
        "used_percent": 3, "limit_window_seconds": seconds, "reset_after_seconds": 10, "reset_at": 1790000100}}}]}
    windows = parse_codex_quota(json.dumps(report, ensure_ascii=False))["windows"]
    assert [window["label"] for window in windows if window["kind"] == "other"] == [f"实验模型 · {label}"]


def test_codex_quota_parser_reads_primary_secondary_and_additional_limits():
    """MH-QUOTA-003: Codex windows classify by length; Spark is named from its metered feature; unknown limits stay generic."""

    parsed = parse_codex_quota(json.dumps(CODEX_REPORT, ensure_ascii=False))
    _validate_windows(parsed)
    assert parsed["plan"] == "pro"
    windows = parsed["windows"]
    assert [(window["kind"], window["label"]) for window in windows] == [
        ("session", "primary_window"),
        ("weekly", "secondary_window"),
        ("model_weekly", "Spark"),
        ("other", "实验模型 · 1h"),
    ]
    assert windows[0]["resets_at"] == "2026-09-21T14:13:20Z"
    assert windows[2]["scope_model"] == "Spark"


def test_codex_quota_parser_classifies_a_lone_weekly_primary_by_length():
    """MH-QUOTA-003: Pro 5x (`prolite`) reports only a weekly window, in the primary slot."""

    report = {
        "plan_type": "prolite",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 31, "limit_window_seconds": 604800,
                               "reset_after_seconds": 400000, "reset_at": 1790400000},
            "secondary_window": None,
        },
        "additional_rate_limits": None,
    }
    parsed = parse_codex_quota(json.dumps(report))
    _validate_windows(parsed)
    assert parsed["plan"] == "prolite"
    assert [(window["id"], window["kind"]) for window in parsed["windows"]] == [("primary_window", "weekly")]


def test_gemini_quota_parser_reads_model_group_windows():
    """MH-QUOTA-037: Antigravity buckets are scoped to their model group, 5-hour before weekly; a spent bucket reads used up, a full one has no moving reset."""

    parsed = parse_gemini_quota(json.dumps(GEMINI_SUMMARY))
    _validate_windows(parsed)
    assert parsed["plan"] is None
    assert [(w["id"], w["kind"], w.get("scope_model"), w["used_pct"], w["resets_at"]) for w in parsed["windows"]] == [
        ("bucket:gemini-5h", "model_session", "Gemini Models", 100.0, "2026-09-25T05:10:00Z"),
        ("bucket:gemini-weekly", "model_weekly", "Gemini Models", 38.0, "2026-09-29T15:52:48Z"),
        ("bucket:3p-5h", "model_session", "Claude and GPT models", 10.0, "2026-09-25T08:00:00Z"),
        # Untouched: Antigravity reports a full window from now, which is no reset at all.
        ("bucket:3p-weekly", "model_weekly", "Claude and GPT models", 0.0, None),
        # A window Antigravity may add later is shown generically, never as an account limit.
        ("bucket:3p-daily", "other", None, 50.0, None),
    ]
    by_id = {w["id"]: w for w in parsed["windows"]}
    assert (by_id["bucket:gemini-5h"]["window_seconds"], by_id["bucket:gemini-weekly"]["window_seconds"]) == (18000, 604800)
    assert by_id["bucket:3p-daily"]["label"] == "Claude and GPT models · daily"
    # A group's own name is display text; nothing here stops the whole account.
    assert not any(w.get("label_is_key") for w in parsed["windows"])
    assert {w["kind"] for w in parsed["windows"]}.isdisjoint({"session", "weekly", "monthly"})

    # The older answer lists bare buckets: the bucket id, less its window, names the group as a key.
    older = {"buckets": [{"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.5,
                          "resetTime": "2026-09-29T00:00:00Z"}]}
    windows = parse_gemini_quota(json.dumps(older))["windows"]
    _validate_windows({"windows": windows})
    assert [(w["kind"], w["scope_model"], w.get("label_is_key")) for w in windows] == [("model_weekly", "gemini", True)]

    # No groups (proto3 sends `{}`), or none with a reading: the vendor reported no quota.
    for empty in ({}, {"groups": []}, {"groups": [{"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-5h", "window": "5h"}, {"bucketId": "gemini-weekly", "window": "weekly", "disabled": True,
                                                       "remainingFraction": 0.1}]}]}):
        assert parse_gemini_quota(json.dumps(empty)) == {"plan": None, "windows": []}


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (GROK_BILLING["config"], ("weekly", "weekly", 37.5, 604800, "2026-09-28T00:00:00Z")),
        ({"creditUsagePercent": 120, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY",
                                                        "start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}},
         ("monthly", "monthly", 100.0, 30 * 86400, "2026-10-01T00:00:00Z")),
        # Grok omits a zero percentage: 0 only beside a well-formed weekly or monthly period.
        ({"currentPeriod": {"type": "MONTHLY", "start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}},
         ("monthly", "monthly", 0.0, 30 * 86400, "2026-10-01T00:00:00Z")),
        ({"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2026-09-28T00:00:00Z"}}, None),
        ({"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-09-28T00:00:00Z",
                            "end": "2026-09-21T00:00:00Z"}}, None),
        ({"currentPeriod": {"type": "USAGE_PERIOD_TYPE_DAILY", "start": "2026-09-25T00:00:00Z",
                            "end": "2026-09-26T00:00:00Z"}}, None),
        ({"creditUsagePercent": None, "currentPeriod": GROK_BILLING["config"]["currentPeriod"]}, None),
        ({}, None),
        # A period this parser does not know is shown generically by its length.
        ({"creditUsagePercent": 12, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_DAILY", "start": "2026-09-25T00:00:00Z",
                                                       "end": "2026-09-26T00:00:00Z"}},
         ("other", "1d", 12.0, 86400, "2026-09-26T00:00:00Z")),
        ({"creditUsagePercent": 12}, ("other", "credit_usage", 12.0, None, None)),
    ],
)
def test_grok_quota_parser_reads_the_one_credit_window(config, expected):
    """MH-QUOTA-038: Grok's credit window is weekly or monthly by its period, resets at the period's end, and is absent rather than invented."""

    parsed = parse_grok_quota(json.dumps({"config": config}))
    _validate_windows(parsed)
    assert parsed["plan"] is None
    if expected is None:
        assert parsed["windows"] == []
        return
    [window] = parsed["windows"]
    assert (window["kind"], window["label"], window["used_pct"], window["window_seconds"], window["resets_at"]) == expected
    assert window.get("label_is_key", False) is (window["label"] == "credit_usage")


@pytest.mark.parametrize(
    ("parser", "body"),
    [
        (parse_gemini_quota, json.dumps({"groups": {"displayName": "Gemini Models"}})),
        (parse_gemini_quota, json.dumps({"buckets": "none"})),
        (parse_gemini_quota, json.dumps({"error": {"code": 403, "message": "Verify your account to continue"}})),
        (parse_gemini_quota, "<html>"),
        (parse_grok_quota, json.dumps({"creditUsagePercent": 5})),
        (parse_grok_quota, json.dumps({"config": [1]})),
        (parse_grok_quota, json.dumps({"error": "WKE=privacy-mode:legacy"})),
        (parse_claude_quota, "not json"),
        (parse_claude_quota, json.dumps([1, 2])),
        (parse_claude_quota, json.dumps({"error": {"type": "authentication_error"}})),
        (parse_claude_quota, json.dumps({"account": {}})),
        (parse_claude_quota, None),
        (parse_codex_quota, json.dumps({"detail": "nope"})),
        (parse_codex_quota, b"\xff\xfe"),
        pytest.param(parse_codex_quota, json.dumps({"additional_rate_limits": [], "pad": "x" * (512 * 1024)}),
                     id="oversized-body"),
        pytest.param(parse_codex_quota, '{"plan_type": "\\ud800", "rate_limit": {}}', id="lone-surrogate"),
    ],
)
def test_quota_parsers_reject_a_malformed_body_as_one_source_failure(parser, body):
    """MH-QUOTA-004: A body that is not the report shape is a sanitized per-Source `malformed` failure."""

    with pytest.raises(SubscriptionQuotaError) as caught:
        parser(body)
    assert caught.value.reason == "malformed"
    assert "nope" not in str(caught.value) and "authentication_error" not in str(caught.value)


def test_quota_parsers_stop_reading_a_runaway_row_list():
    """MH-QUOTA-004: A body with a huge row list costs a bounded read, not one row per upstream entry."""

    row = {"kind": "weekly_scoped", "percent": 5, "scope": {"model": {"display_name": "M"}}}
    parsed = parse_claude_quota(json.dumps({"limits": [row] * 2_000}))
    assert len(parsed["windows"]) == 1  # dedup by id; the slice is what bounds the work
    additional = [{"limit_name": f"m{i}", "rate_limit": {"primary_window": {"used_percent": 1, "limit_window_seconds": 3600}}}
                  for i in range(2_000)]
    parsed = parse_codex_quota(json.dumps({"additional_rate_limits": additional}))
    assert len(parsed["windows"]) == 16
    bucket = {"window": "5h", "remainingFraction": 0.5}
    for groups in ([{"buckets": [{**bucket, "bucketId": f"g{i}"}]} for i in range(2_000)],
                   [{"buckets": [{**bucket, "bucketId": f"b{j}"} for j in range(2_000)]}]):
        parsed = parse_gemini_quota(json.dumps({"groups": groups}, separators=(",", ":")))
        assert len(parsed["windows"]) == 16


def test_quota_parsers_bound_values_from_a_hostile_body():
    """MH-QUOTA-004: Percentages clamp to 0–100, non-finite values drop, and labels are bounded."""

    body = {
        "five_hour": {"utilization": 250, "resets_at": "garbage"},
        "seven_day": {"utilization": float("nan")},
        "limits": [],
        "x" * 300: {"utilization": -5, "resets_at": None},
    }
    parsed = parse_claude_quota(json.dumps(body))
    _validate_windows(parsed)
    assert parsed["windows"][0] == {
        "id": "five_hour", "kind": "session", "label": "five_hour", "used_pct": 100.0,
        "window_seconds": 18000, "resets_at": None,
    }
    assert parsed["windows"][1]["used_pct"] == 0.0
    assert len(parsed["windows"][1]["label"]) == 64
    assert all(window["id"] != "seven_day" for window in parsed["windows"])

    # A composed label and an out-of-range timestamp stay inside the contract.
    codex = {"additional_rate_limits": [{"limit_name": "m" * 64, "rate_limit": {"primary_window": {
        "used_percent": 1, "limit_window_seconds": 3600, "reset_at": 10**12}}}]}
    parsed = parse_codex_quota(json.dumps(codex))
    _validate_windows(parsed)
    assert len(parsed["windows"][0]["label"]) == 64
    parsed = parse_claude_quota(json.dumps({"five_hour": {"utilization": 1, "resets_at": "9999-12-31T23:59:00-05:00"}}))
    assert parsed["windows"][0]["resets_at"] is None

    # A limit with headroom never rounds up to the spent sentinel; a sub-second span is unreadable.
    codex = {"rate_limit": {"primary_window": {"used_percent": 99.96, "limit_window_seconds": 0.5, "reset_at": 0.5}}}
    parsed = parse_codex_quota(json.dumps(codex))
    _validate_windows(parsed)
    assert [(w["used_pct"], w["window_seconds"], w["resets_at"]) for w in parsed["windows"]] == [(99.9, None, None)]

    # A JSON integer too large for a float is a missing value, not a failed Source.
    huge = 10**4000
    parsed = parse_claude_quota(json.dumps({"five_hour": {"utilization": huge}, "seven_day": {"utilization": 3}}))
    assert [w["id"] for w in parsed["windows"]] == ["seven_day"]
    codex = {"rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": huge, "reset_at": huge}}}
    parsed = parse_codex_quota(json.dumps(codex))
    _validate_windows(parsed)
    assert [(w["used_pct"], w["window_seconds"], w["resets_at"]) for w in parsed["windows"]] == [(5.0, None, None)]

    # Antigravity fractions clamp to 0–1, an unreadable one drops, and a long group name is bounded.
    hostile = {"groups": [{"displayName": "G" * 300, "buckets": [
        {"bucketId": "a", "window": "5h", "remainingFraction": -3, "resetTime": "2026-09-25T05:00:00Z"},
        {"bucketId": "b", "window": "weekly", "remainingFraction": 7},
        {"bucketId": "c", "window": "5h", "remainingFraction": "half", "resetTime": "2026-09-25T05:00:00Z"},
        {"bucketId": "d", "window": "weekly", "remainingFraction": huge, "resetTime": "2026-09-29T00:00:00Z"},
        {"bucketId": "e", "window": "weekly", "resetTime": "garbage"},
    ]}]}
    parsed = parse_gemini_quota(json.dumps(hostile))
    _validate_windows(parsed)
    assert [(w["id"], w["used_pct"], len(w["scope_model"])) for w in parsed["windows"]] == [
        ("bucket:a", 100.0, 64), ("bucket:b", 0.0, 64)]
    # A Grok percent past 100 is spent, never more; an impossible date is no period.
    parsed = parse_grok_quota(json.dumps({"config": {"creditUsagePercent": 400,
                                                     "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                                                       "start": "2026-02-30T00:00:00Z",
                                                                       "end": "2026-03-07T00:00:00Z"}}}))
    _validate_windows(parsed)
    assert [(w["used_pct"], w["window_seconds"]) for w in parsed["windows"]] == [(100.0, None)]

    # Unknown metadata ahead of Claude's fixed keys does not crowd them out.
    padded = {f"meta{i}": {} for i in range(100)} | {"five_hour": {"utilization": 7}}
    assert [w["id"] for w in parse_claude_quota(json.dumps(padded))["windows"]] == ["five_hour"]


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class _Fetch:
    def __init__(self):
        self.calls = []
        self.results = []

    async def __call__(self, source_id, vendor, credential_ref):
        self.calls.append((source_id, vendor, credential_ref))
        result = self.results.pop(0) if self.results else {"plan": "max", "windows": [_WINDOW]}
        if isinstance(result, BaseException):
            raise result
        return result


_WINDOW = {"id": "five_hour", "kind": "session", "label": "five_hour", "used_pct": 10.0,
           "window_seconds": 18000, "resets_at": "2026-09-25T05:00:00Z"}
_CLAUDE = QuotaSourceRef("src_claude", "anthropic", "cred_a", "Claude Max", "alex@example.com")
_CODEX = QuotaSourceRef("src_codex", "openai", "cred_b", "ChatGPT Pro", None)
_OTHER = QuotaSourceRef("src_kimi", "kimi", "cred_c", "Kimi", None)


def _validate_summary(summary):
    Draft7Validator(QUOTA_SCHEMA).validate(summary)


async def test_quota_cache_refreshes_each_source_at_most_every_five_minutes():
    """MH-QUOTA-005: A read re-fetches a Source only after the refresh interval; concurrent reads share one fetch."""

    clock, fetch = _Clock(), _Fetch()
    cache = SubscriptionQuotaCache(fetch, now=clock)
    first, second = await asyncio.gather(cache.summary([_CLAUDE, _CODEX]), cache.summary([_CLAUDE, _CODEX]))
    _validate_summary(first)
    assert first == second
    assert len(fetch.calls) == 2
    assert first["refresh_interval_seconds"] == 300
    assert [source["state"] for source in first["sources"]] == ["ok", "ok"]
    assert first["sources"][0]["fetched_at"] == "2026-09-25T03:00:00Z"
    assert first["sources"][0]["account_label"] == "alex@example.com"

    clock.now += QUOTA_REFRESH_INTERVAL - timedelta(seconds=1)
    await cache.summary([_CLAUDE, _CODEX])
    assert len(fetch.calls) == 2
    clock.now += timedelta(seconds=1)
    await cache.summary([_CLAUDE, _CODEX])
    assert len(fetch.calls) == 4


async def test_quota_cache_keeps_the_last_good_snapshot_as_stale():
    """MH-QUOTA-006: A failed re-read keeps the last good windows and reports the Source stale; the next good read clears it."""

    clock, fetch = _Clock(), _Fetch()
    cache = SubscriptionQuotaCache(fetch, now=clock)
    await cache.summary([_CLAUDE])
    fetch.results = [SubscriptionQuotaError("unavailable")]
    clock.now += QUOTA_REFRESH_INTERVAL
    summary = await cache.summary([_CLAUDE])
    _validate_summary(summary)
    source = summary["sources"][0]
    assert source["state"] == "stale"
    assert source["error_key"] == "models.quota.error.unavailable"
    assert source["windows"] == [_WINDOW]
    assert source["fetched_at"] == "2026-09-25T03:00:00Z"

    clock.now += QUOTA_REFRESH_INTERVAL
    recovered = (await cache.summary([_CLAUDE]))["sources"][0]
    assert recovered["state"] == "ok" and "error_key" not in recovered


async def test_quota_cache_reports_auth_expired_and_never_a_page_failure():
    """MH-QUOTA-007: A refused grant is auth_expired, an unexpected error is one Source's error, and other Sources still answer."""

    clock, fetch = _Clock(), _Fetch()
    cache = SubscriptionQuotaCache(fetch, now=clock)
    fetch.results = [SubscriptionQuotaError("auth_expired"), RuntimeError("secret body text")]
    summary = await cache.summary([_CLAUDE, _CODEX, _OTHER])
    _validate_summary(summary)
    claude, codex, other = summary["sources"]
    assert claude["state"] == "auth_expired" and claude["windows"] == []
    assert claude["error_key"] == "models.quota.error.auth_expired"
    assert codex["state"] == "error" and codex["error_key"] == "models.quota.error.unavailable"
    assert other["state"] == "unsupported"
    assert "secret body text" not in json.dumps(summary)
    assert [call[0] for call in fetch.calls] == ["src_claude", "src_codex"]


async def test_quota_cache_forced_refresh_is_rate_limited_and_429_cools_down():
    """MH-QUOTA-008: A forced refresh re-reads at most every 30 s; a vendor 429 suppresses reads until its cooldown ends."""

    clock, fetch = _Clock(), _Fetch()
    cache = SubscriptionQuotaCache(fetch, now=clock)
    await cache.summary([_CLAUDE])
    await cache.summary([_CLAUDE], force=True)
    assert len(fetch.calls) == 1
    clock.now += QUOTA_FORCED_REFRESH_INTERVAL
    fetch.results = [SubscriptionQuotaError("rate_limited", retry_after_seconds=600)]
    limited = (await cache.summary([_CLAUDE], force=True))["sources"][0]
    assert len(fetch.calls) == 2
    assert limited["state"] == "stale" and limited["error_key"] == "models.quota.error.rate_limited"

    clock.now += timedelta(minutes=9)
    await cache.summary([_CLAUDE], force=True)
    assert len(fetch.calls) == 2
    clock.now += timedelta(minutes=1)
    assert (await cache.summary([_CLAUDE]))["sources"][0]["state"] == "ok"
    assert len(fetch.calls) == 3


async def test_quota_cache_caps_a_huge_retry_after_at_the_ceiling():
    """MH-QUOTA-008: A hostile Retry-After records the one-hour ceiling instead of overflowing the refresh."""

    clock, fetch = _Clock(), _Fetch()
    cache = SubscriptionQuotaCache(fetch, now=clock)
    await cache.summary([_CLAUDE])
    clock.now += QUOTA_REFRESH_INTERVAL
    fetch.results = [SubscriptionQuotaError("rate_limited", retry_after_seconds=1e15)]
    limited = (await cache.summary([_CLAUDE]))["sources"][0]
    assert limited["error_key"] == "models.quota.error.rate_limited"
    clock.now += timedelta(minutes=59)
    await cache.summary([_CLAUDE], force=True)
    assert len(fetch.calls) == 2
    clock.now += timedelta(minutes=1)
    await cache.summary([_CLAUDE], force=True)
    assert len(fetch.calls) == 3


async def test_quota_cache_bounds_concurrent_vendor_fetches():
    """MH-QUOTA-005: Many Sources queue behind a fixed number of in-flight fetches; each still gets its own read."""

    active, peak, gate = 0, 0, asyncio.Event()

    async def fetch(source_id, vendor, credential_ref):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate.wait()
        active -= 1
        return {"plan": None, "windows": [_WINDOW]}

    cache = SubscriptionQuotaCache(fetch, now=_Clock())
    sources = [QuotaSourceRef(f"src_{i}", "anthropic", f"cred_{i}", f"S{i}", None) for i in range(12)]
    reading = asyncio.create_task(cache.summary(sources))
    for _ in range(5):
        await asyncio.sleep(0)
    assert peak == 4
    gate.set()
    summary = await reading
    assert peak == 4 and [source["state"] for source in summary["sources"]] == ["ok"] * 12


async def test_quota_cache_throttles_from_the_vendor_request_not_the_queue():
    """MH-QUOTA-008: A read that waited for a fetch slot starts its forced-refresh cooldown when it asks the vendor."""

    clock, gate, calls = _Clock(), asyncio.Event(), []

    async def fetch(source_id, vendor, credential_ref):
        calls.append(source_id)
        if len(calls) <= 4:
            await gate.wait()
        return {"plan": None, "windows": [_WINDOW]}

    cache = SubscriptionQuotaCache(fetch, now=clock)
    sources = [QuotaSourceRef(f"src_{i}", "anthropic", f"cred_{i}", f"S{i}", None) for i in range(5)]
    reading = asyncio.create_task(cache.summary(sources))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls == ["src_0", "src_1", "src_2", "src_3"]
    # The fifth read sits in the queue past the forced-refresh interval.
    clock.now += QUOTA_FORCED_REFRESH_INTERVAL * 2
    gate.set()
    await reading
    assert calls[-1] == "src_4"
    await cache.summary(sources, force=True)
    # The four that asked the vendor a minute ago re-read; the one that only just asked does not.
    assert sorted(calls[5:]) == ["src_0", "src_1", "src_2", "src_3"]


async def test_quota_cache_forgets_a_reauthenticated_grant():
    """MH-QUOTA-009: A Source bound to a new grant does not inherit the old grant's windows or failure."""

    clock, fetch = _Clock(), _Fetch()
    cache = SubscriptionQuotaCache(fetch, now=clock)
    fetch.results = [SubscriptionQuotaError("auth_expired")]
    await cache.summary([_CLAUDE])
    rebound = QuotaSourceRef("src_claude", "anthropic", "cred_new", "Claude Max", None)
    summary = await cache.summary([rebound])
    assert summary["sources"][0]["state"] == "ok"
    assert fetch.calls[-1] == ("src_claude", "anthropic", "cred_new")


async def test_quota_cache_does_not_hold_the_page_on_a_slow_vendor(monkeypatch):
    """MH-QUOTA-010: A vendor that has not answered by the read deadline leaves its Source unread without delaying the others."""

    from core.handlers.model_hub import quota

    monkeypatch.setattr(quota, "QUOTA_READ_DEADLINE_SECONDS", 0.05)
    release = asyncio.Event()

    async def fetch(source_id, vendor, credential_ref):
        if source_id == "src_codex":
            await release.wait()
        return {"plan": None, "windows": [_WINDOW]}

    cache = SubscriptionQuotaCache(fetch, now=_Clock())
    summary = await cache.summary([_CLAUDE, _CODEX])
    assert [source["state"] for source in summary["sources"]] == ["ok", "error"]
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [source["state"] for source in (await cache.summary([_CLAUDE, _CODEX]))["sources"]] == ["ok", "ok"]


async def test_quota_summary_names_the_sources_still_being_read(monkeypatch):
    """MH-QUOTA-020: A read past the deadline is named `pending` until it lands, then the hint is gone."""

    from core.handlers.model_hub import quota

    monkeypatch.setattr(quota, "QUOTA_READ_DEADLINE_SECONDS", 0.05)
    release = asyncio.Event()

    async def fetch(source_id, vendor, credential_ref):
        if source_id == "src_codex":
            await release.wait()
        return {"plan": None, "windows": [_WINDOW]}

    cache = SubscriptionQuotaCache(fetch, now=_Clock())
    first = await cache.summary([_CLAUDE, _CODEX])
    assert first["pending"] == ["src_codex"]
    # A re-read while the vendor is still slow says so again, without a second fetch.
    assert (await cache.summary([_CLAUDE, _CODEX]))["pending"] == ["src_codex"]
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    settled = await cache.summary([_CLAUDE, _CODEX])
    assert "pending" not in settled
    assert [source["state"] for source in settled["sources"]] == ["ok", "ok"]


class _Supervisor:
    def __init__(self, client):
        self._client = client

    def client_if_running(self):
        return self._client

    def client(self):  # pragma: no cover - a presentation read must never call this
        raise AssertionError("quota read must not start the engine")


class _EngineClient:
    def __init__(self, response, by_url=None):
        self.response = response
        self.by_url = by_url or {}
        self.calls = []

    def management_request(self, method, path, *, query=None, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if path == "/auth-files":
            return {"files": [
                {"auth_index": "7", "id": "claude-a.json", "name": "claude-a.json", "provider": "claude"},
                {"auth_index": "8", "id": "codex-b.json", "name": "codex-b.json", "provider": "codex",
                 "id_token": {"chatgpt_account_id": "acct_123"}},
                {"auth_index": "9", "id": "antigravity-c.json", "name": "antigravity-c.json", "provider": "antigravity",
                 "email": "someone@example.com", "project_id": _PROJECT_ID},
                {"auth_index": "10", "id": "xai-d.json", "name": "xai-d.json", "provider": "xai"},
                {"auth_index": "11", "id": "antigravity-e.json", "name": "antigravity-e.json", "provider": "antigravity"},
            ]}
        answer = self.by_url.get((payload or {}).get("url"), self.response)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _StateStore:
    def __init__(self, metadata):
        self.metadata = metadata

    def credential_metadata(self, credential_ref):
        return self.metadata[credential_ref]


def _adapter(response, metadata, by_url=None):
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter

    client = _EngineClient(response, by_url)
    adapter = CLIProxyEngineAdapter(supervisor=_Supervisor(client), state_store=_StateStore(metadata))
    return adapter, client


_PROJECT_ID = "made-up-project-4711"
_METADATA = {
    "cred_a": {"kind": "oauth", "vendor": "anthropic", "auth_name": "claude-a.json", "source_id": "src_claude"},
    "cred_b": {"kind": "oauth", "vendor": "openai", "auth_name": "codex-b.json", "source_id": "src_codex"},
    "cred_c": {"kind": "oauth", "vendor": "gemini", "auth_name": "antigravity-c.json", "source_id": "src_gemini"},
    "cred_d": {"kind": "oauth", "vendor": "xai", "auth_name": "xai-d.json", "source_id": "src_grok"},
    "cred_e": {"kind": "oauth", "vendor": "gemini", "auth_name": "antigravity-e.json", "source_id": "src_gemini"},
}
_GEMINI_DAILY = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
_GEMINI_PROD = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
_GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"


async def test_adapter_reads_quota_through_the_engine_without_exposing_the_grant():
    """MH-QUOTA-011: The engine makes the usage call with `$TOKEN$`, the witness headers, and Claude's OAuth beta; only parsed windows return."""

    adapter, client = _adapter(
        {"status_code": 200, "header": {}, "body": json.dumps(CLAUDE_LEGACY)},
        _METADATA,
        {_PROFILE_URL: {"status_code": 200, "body": "{}"}},
    )
    parsed = await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
    assert set(parsed) == {"plan", "windows", "reset_cards", "reset_card_account"}
    # A profile that names no organization names no account either.
    assert parsed["reset_card_account"] is None
    usage_calls = [call for call in client.calls if call[1] == '/api-call' and call[2]['url'] != _PROFILE_URL]
    method, path, payload = usage_calls[0]
    assert usage_calls[1][2] == {**payload, 'url': payload['url'] + '?cedar_ember=1&skip_spend=1'}
    assert (method, path) == ("POST", "/api-call")
    assert payload["auth_index"] == "7"
    assert payload["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert payload["header"]["Authorization"] == "Bearer $TOKEN$"
    assert payload["header"]["anthropic-beta"] == "oauth-2025-04-20"
    assert payload["header"]["User-Agent"] == "axios/1.15.2"

    adapter, client = _adapter({"status_code": 200, "header": {}, "body": json.dumps(CODEX_REPORT)}, _METADATA)
    parsed = await adapter.subscription_quota("src_codex", "openai", "cred_b")
    # The account a reading was made under leaves as an opaque key, never as the vendor's id.
    assert re.fullmatch("[a-f0-9]{64}", parsed["reset_card_account"]) and "acct_123" not in json.dumps(parsed)
    payload = client.calls[-1][2]
    assert payload["auth_index"] == "8"
    assert payload["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert payload["header"]["ChatGPT-Account-ID"] == "acct_123"
    assert payload["header"]["User-Agent"] == "codex-cli"
    assert "anthropic-beta" not in payload["header"]


async def test_chatgpt_cards_use_usage_count_and_only_available_codex_details():
    """MH-QUOTA-042: ChatGPT reports its usage count, not a detail row count, and never promises a stated effect."""
    usage = {**CODEX_REPORT, "rate_limit_reset_credits": {"available_count": 3}}
    credit = {
        "id": "private-marker",
        "status": "available",
        "reset_type": "codex_rate_limits",
        "expires_at": "2090-01-01T00:00:00Z",
    }
    details = {
        "credits": [
            credit,
            {**credit, "reset_type": "other", "expires_at": "2080-01-01T00:00:00Z"},
            {**credit, "status": "redeemed", "expires_at": "2070-01-01T00:00:00Z"},
        ]
    }
    adapter, client = _adapter(
        {"status_code": 200, "body": json.dumps(usage)},
        _METADATA,
        {
            "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits": {
                "status_code": 200,
                "body": json.dumps(details),
            }
        },
    )
    cache = SubscriptionQuotaCache(adapter.subscription_quota, now=_Clock())
    summary = await cache.summary([QuotaSourceRef("src_codex", "openai", "cred_b", "Personal", None)])
    assert summary["sources"][0]["reset_cards"] == {
        "state": "ok",
        "lock": None,
        "cards": [
            {
                "kind": "account",
                "count": 3,
                "usable": True,
                "blocked": None,
                "stated": False,
                "blocked_until": None,
                "expires_at": "2090-01-01T00:00:00Z",
            }
        ],
    }
    serialized = json.dumps(summary)
    for sentinel in ("private-marker", "acct_123", "$TOKEN$", "auth_index"):
        assert sentinel not in serialized
    assert all(call[2]["method"] == "GET" for call in client.calls if call[1] == "/api-call")
    Draft7Validator(QUOTA_SCHEMA).validate(summary)


async def test_card_details_failure_preserves_current_windows(caplog):
    """MH-QUOTA-044: A cards-only failure returns unread without discarding quota or exposing the error."""
    usage = {**CODEX_REPORT, "rate_limit_reset_credits": {"available_count": 2}}
    from vibe.model_hub_runtime.client import EngineClientError

    adapter, client = _adapter(
        {"status_code": 200, "body": json.dumps(usage)},
        _METADATA,
        {
            "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits": EngineClientError("private-marker"),
        },
    )
    cache = SubscriptionQuotaCache(adapter.subscription_quota, now=_Clock())
    source = QuotaSourceRef("src_codex", "openai", "cred_b", "Personal", None)
    summary = await cache.summary([source])
    row = summary["sources"][0]
    assert row["state"] == "ok" and row["windows"]
    assert row["reset_cards"] == {"state": "unread", "lock": None, "cards": []}
    assert "private-marker" not in json.dumps(summary) + caplog.text
    assert all(payload["method"] == "GET" for _, path, payload in client.calls if path == "/api-call")


async def test_claude_cards_group_by_kind_without_exposing_grants():
    """MH-QUOTA-043: A grouped card has only closed fields and counts held grants, not vendor labels or identities."""
    grant = {
        "id": "private-marker",
        "label": "private-marker",
        "type": "full",
        "resets_left": 2,
        "usable_now": True,
        "clears": ["five_hour", "seven_day"],
        "ends_at": "2090-01-01T00:00:00Z",
    }
    program = {
        "eligible": True,
        "at_limit": True,
        "grants": [
            grant,
            {**grant, "id": "second", "resets_left": 3},
            {**grant, "id": "third", "paused": True},
            {**grant, "id": "fourth", "ends_at": "2000-01-01T00:00:00Z"},
        ],
    }
    adapter, _ = _adapter(
        {"status_code": 200, "body": json.dumps({**CLAUDE_LEGACY, "cedar_ember": program})}, _METADATA
    )
    cache = SubscriptionQuotaCache(adapter.subscription_quota, now=_Clock())
    summary = await cache.summary([QuotaSourceRef("src_claude", "anthropic", "cred_a", "Personal", None)])
    cards = summary["sources"][0]["reset_cards"]
    assert cards == {
        "state": "ok",
        "lock": None,
        "cards": [
            {
                "kind": "all",
                "count": 5,
                "expires_at": "2090-01-01T00:00:00Z",
                "stated": True,
                "usable": True,
                "blocked": None,
                "blocked_until": None,
            }
        ],
    }
    assert "private-marker" not in json.dumps(summary)
    Draft7Validator(QUOTA_SCHEMA).validate(summary)


@pytest.mark.parametrize(
    ("patch", "extra", "kind", "blocked", "stated"),
    [
        ({"clears": ["five_hour"]}, {}, "session", "needs_limit", True),
        ({"clears": ["seven_day"]}, {}, "weekly", "needs_limit", True),
        ({"type": "full", "clears": ["seven_day"]}, {}, "all", "needs_limit", False),
        ({"gives_back_weekly": True}, {"cooldown_until": "2090-01-01T00:00:00Z"}, "fable", "cooldown", True),
        ({"use_requires_limit": False, "blocking": ["private-marker"]}, {}, "session", "unavailable", True),
    ],
)
async def test_cards_keep_blocked_grants_and_report_only_vendor_stated_effects(patch, extra, kind, blocked, stated):
    grant = {"id": "private-marker", "resets_left": 1, "usable_now": True, "clears": ["five_hour"], **patch}
    block = {"eligible": True, "at_limit": False, "grants": [grant], **extra}
    adapter, _ = _adapter({"status_code": 200, "body": json.dumps({**CLAUDE_LEGACY, "cedar_ember": block})}, _METADATA)
    parsed = await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
    assert parsed["reset_cards"]["cards"] == [
        dict(
            kind=kind,
            count=1,
            usable=False,
            blocked=blocked,
            blocked_until=extra.get("cooldown_until"),
            expires_at=None,
            stated=stated,
        )
    ]


@pytest.mark.parametrize(
    "malformed",
    [
        False,
        [],
        {"grants": {}},
        {"grants": [{"resets_left": True}]},
        {"grants": [], "cooldown_until": "private-marker"},
    ],
)
async def test_hostile_card_fields_never_escape_or_break_windows(malformed, caplog):
    adapter, _ = _adapter(
        {"status_code": 200, "body": json.dumps({**CLAUDE_LEGACY, "cedar_ember": malformed})}, _METADATA
    )
    parsed = await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
    assert parsed["reset_cards"] == {"state": "unread", "lock": None, "cards": []}
    assert parsed["windows"] and "private-marker" not in json.dumps(parsed) + caplog.text


async def test_adapter_reads_gemini_and_grok_quota_as_the_engine_itself_would():
    """MH-QUOTA-039: Antigravity is asked per project with the engine's own client identity, falling back to prod only when daily does not answer; Grok's billing read carries the engine's Grok CLI identity; nothing of the grant or project comes back."""

    def answer(body, status=200):
        return {"status_code": status, "header": {}, "body": json.dumps(body)}

    adapter, client = _adapter(answer(GROK_BILLING), _METADATA, {_GEMINI_DAILY: answer(GEMINI_SUMMARY)})
    parsed = await adapter.subscription_quota("src_gemini", "gemini", "cred_c")
    assert set(parsed) == {"plan", "windows"} and parsed["windows"]
    assert _PROJECT_ID not in json.dumps(parsed)
    reads = [call for call in client.calls if call[1] == "/api-call"]
    assert len(reads) == 1
    method, path, payload = reads[0]
    assert (method, path) == ("POST", "/api-call")
    assert payload == {
        "auth_index": "9",
        "method": "POST",
        "url": _GEMINI_DAILY,
        "header": {
            "Content-Type": "application/json",
            "Authorization": "Bearer $TOKEN$",
            "User-Agent": "antigravity/hub/2.34.0 (aidev_client; os_type=darwin; arch=arm64; cl=1026637980)",
        },
        "data": json.dumps({"project": _PROJECT_ID}, separators=(",", ":")),
    }

    await adapter.subscription_quota("src_grok", "xai", "cred_d")
    method, path, payload = client.calls[-1]
    assert payload == {
        "auth_index": "10",
        "method": "GET",
        "url": _GROK_BILLING_URL,
        "header": {
            "Accept": "application/json",
            "Authorization": "Bearer $TOKEN$",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": "1.0.46",
            "User-Agent": "xai-grok-workspace/1.0.46",
            "x-grok-client-identifier": "grok-shell",
        },
    }

    # Both reach the page as readings, in the contract's shape, with nothing of the account in them.
    cache = SubscriptionQuotaCache(adapter.subscription_quota, now=_Clock())
    summary = await cache.summary([QuotaSourceRef("src_gemini", "gemini", "cred_c", "Gemini", None),
                                   QuotaSourceRef("src_grok", "xai", "cred_d", "Grok", None)])
    _validate_summary(summary)
    assert [(source["state"], len(source["windows"])) for source in summary["sources"]] == [("ok", 5), ("ok", 1)]
    assert _PROJECT_ID not in json.dumps(summary)

    # Daily not answering sends the same read to prod; a refusal or a 429 does not.
    for status in (404, 408, 503):
        adapter, client = _adapter(answer(GEMINI_SUMMARY), _METADATA, {_GEMINI_DAILY: answer({}, status)})
        assert (await adapter.subscription_quota("src_gemini", "gemini", "cred_c"))["windows"]
        urls = [call[2]["url"] for call in client.calls if call[1] == "/api-call"]
        assert urls == [_GEMINI_DAILY, _GEMINI_PROD]
        assert client.calls[-1][2]["data"] == json.dumps({"project": _PROJECT_ID}, separators=(",", ":"))
    for status, reason in ((403, "auth_expired"), (429, "rate_limited")):
        adapter, client = _adapter(answer(GEMINI_SUMMARY), _METADATA, {_GEMINI_DAILY: answer({}, status)})
        with pytest.raises(SubscriptionQuotaError) as caught:
            await adapter.subscription_quota("src_gemini", "gemini", "cred_c")
        assert caught.value.reason == reason
        assert [call[2]["url"] for call in client.calls if call[1] == "/api-call"] == [_GEMINI_DAILY]
    adapter, client = _adapter(answer({}, 503), _METADATA)
    with pytest.raises(SubscriptionQuotaError) as caught:
        await adapter.subscription_quota("src_gemini", "gemini", "cred_c")
    assert caught.value.reason == "unavailable"

    # A grant without a project has nothing to ask: no call, and a report of no quota.
    adapter, client = _adapter(answer(GEMINI_SUMMARY), _METADATA)
    assert await adapter.subscription_quota("src_gemini", "gemini", "cred_e") == {"plan": None, "windows": []}
    assert not [call for call in client.calls if call[1] == "/api-call"]

    from vibe.model_hub_runtime.adapter import _auth_inventory

    # The project id stays out of the auth record's printed form.
    inventory = _auth_inventory(_EngineClient({}))
    assert inventory["antigravity-c.json"].project_id == _PROJECT_ID
    assert _PROJECT_ID not in repr(inventory)


_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


# The profile shape the Claude Code CLI (2.1.280) reads: `organization_type`
# names the product and `rate_limit_tier` the Max tier; identifiers are fake.
CLAUDE_PROFILE = {
    "account": {
        "uuid": "00000000-0000-4000-8000-000000000001",
        "email": "someone@example.com",
        "full_name": "Someone",
        "display_name": "Someone",
        "has_claude_max": True,
        "has_claude_pro": False,
    },
    "organization": {
        "uuid": "00000000-0000-4000-8000-000000000002",
        "name": "someone@example.com's Organization",
        "organization_type": "claude_max",
        "billing_type": "stripe_subscription",
        "rate_limit_tier": "default_claude_max_20x",
        "seat_tier": None,
        "has_extra_usage_enabled": False,
    },
}


@pytest.mark.parametrize(
    ("organization", "plan"),
    [
        ({}, "max_20x"),
        ({"rate_limit_tier": "default_claude_max_5x"}, "max_5x"),
        ({"organization_type": "claude_pro", "rate_limit_tier": "default_claude_ai"}, "pro"),
        ({"organization_type": "claude_team", "rate_limit_tier": "default_claude_max_5x"}, "team"),
        ({"organization_type": "Claude_Enterprise", "rate_limit_tier": "default_claude_max_20x"}, "enterprise"),
        ({"rate_limit_tier": "something_new"}, "max"),
        ({"rate_limit_tier": None}, "max"),
        ({"organization_type": "  "}, None),
        ({"organization_type": None}, None),
    ],
)
def test_claude_plan_is_read_from_the_oauth_profile_organization(organization, plan):
    """MH-QUOTA-019, MH-QUOTA-030: organization_type names the product and rate_limit_tier the Max tier; a profile naming no product is no plan."""

    from core.handlers.model_hub.quota import parse_claude_plan

    body = {**CLAUDE_PROFILE, "organization": {**CLAUDE_PROFILE["organization"], **organization}}
    assert parse_claude_plan(json.dumps(body)) == plan


def test_claude_plan_ignores_an_unreadable_profile():
    """MH-QUOTA-019: A body that is not a profile yields no plan, never an error."""

    from core.handlers.model_hub.quota import parse_claude_plan

    assert parse_claude_plan("<html>") is None
    assert parse_claude_plan(json.dumps({"account": {}, "organization": "not an object"})) is None


async def test_adapter_adds_the_claude_plan_from_the_profile_and_never_fails_on_it():
    """MH-QUOTA-019: The plan is a second, best-effort read with the witness headers; its failure leaves windows intact."""

    usage = {"status_code": 200, "header": {}, "body": json.dumps(CLAUDE_LEGACY)}
    body = {**CLAUDE_PROFILE, "organization": {**CLAUDE_PROFILE["organization"], "rate_limit_tier": "default_claude_max_5x"}}
    profile = {"status_code": 200, "body": json.dumps(body)}
    adapter, client = _adapter(usage, _METADATA, {_PROFILE_URL: profile})
    parsed = await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
    assert parsed["plan"] == "max_5x" and parsed["windows"]
    profile_call = client.calls[-1][2]
    assert profile_call["url"] == _PROFILE_URL
    assert profile_call["header"]["Authorization"] == "Bearer $TOKEN$"

    from vibe.model_hub_runtime.client import EngineClientError

    for failure in ({"status_code": 500, "body": "{}"}, {"status_code": 200, "body": "<html>"}, EngineClientError("down")):
        adapter, _client = _adapter(usage, _METADATA, {_PROFILE_URL: failure})
        parsed = await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
        assert parsed["plan"] is None and parsed["windows"]


async def test_a_readable_claude_profile_replaces_the_plan_and_an_unreadable_one_keeps_it():
    """MH-QUOTA-019, MH-QUOTA-030: Only a failed plan read keeps the last plan; a readable one replaces it, fee or none."""

    usage = {"status_code": 200, "header": {}, "body": json.dumps(CLAUDE_LEGACY)}

    def profile(**organization):
        body = {**CLAUDE_PROFILE, "organization": {**CLAUDE_PROFILE["organization"], **organization}}
        return {"status_code": 200, "body": json.dumps(body)}

    profiles = {_PROFILE_URL: profile()}
    adapter, _client = _adapter(usage, _METADATA, profiles)
    moment = [datetime(2026, 9, 1, tzinfo=timezone.utc)]
    cache = SubscriptionQuotaCache(adapter.subscription_quota, now=lambda: moment[0])

    async def plan_after(answer):
        profiles[_PROFILE_URL] = answer
        moment[0] += timedelta(minutes=6)
        source = (await cache.summary([_CLAUDE]))["sources"][0]
        assert source["state"] == "ok" and source["windows"]
        return source["plan"]

    assert (await cache.summary([_CLAUDE]))["sources"][0]["plan"] == "max_20x"
    for failure in ({"status_code": 500, "body": "{}"}, {"status_code": 200, "body": "<html>"}):
        assert await plan_after(failure) == "max_20x"
    # Moving to a Team seat names Team, which has no built-in fee to compare with.
    assert await plan_after(profile(organization_type="claude_team")) == "team"
    assert await plan_after(profile(organization_type="claude_max", rate_limit_tier="default_claude_max_5x")) == "max_5x"
    assert await plan_after(profile(organization_type="claude_pro", rate_limit_tier="default_claude_ai")) == "pro"
    # A Max tier the fee table does not know is still Max, never the last known tier.
    assert await plan_after(profile(organization_type="claude_max", rate_limit_tier="default_claude_max_40x")) == "max"


@pytest.mark.parametrize(
    ("response", "reason", "retry_after"),
    [
        ({"status_code": 401, "body": "{}"}, "auth_expired", None),
        ({"status_code": 403, "body": "{}"}, "auth_expired", None),
        ({"status_code": 429, "header": {"Retry-After": ["120"]}, "body": "{}"}, "rate_limited", 120.0),
        ({"status_code": 429, "header": {"retry-after": ["Wed, 21 Oct 2015 07:28:00 GMT"]}, "body": "{}"}, "rate_limited", None),
        ({"status_code": 429, "header": {"Retry-After": "soon"}, "body": "{}"}, "rate_limited", None),
        ({"status_code": 500, "body": "{}"}, "unavailable", None),
        ({"status_code": 200, "body": "<html>"}, "malformed", None),
    ],
)
async def test_adapter_maps_vendor_statuses_to_sanitized_quota_failures(response, reason, retry_after):
    """MH-QUOTA-011: Vendor statuses become sanitized reasons; a 429 carries its Retry-After."""

    adapter, _client = _adapter(response, _METADATA)
    with pytest.raises(SubscriptionQuotaError) as caught:
        await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
    assert caught.value.reason == reason
    assert caught.value.retry_after_seconds == retry_after


async def test_adapter_honours_a_future_http_date_retry_after():
    """MH-QUOTA-011: A 429 whose Retry-After is an HTTP-date carries the delay until that date."""

    from email.utils import format_datetime

    later = datetime.now(timezone.utc) + timedelta(minutes=40)
    adapter, _client = _adapter(
        {"status_code": 429, "header": {"Retry-After": [format_datetime(later, usegmt=True)]}, "body": "{}"}, _METADATA
    )
    with pytest.raises(SubscriptionQuotaError) as caught:
        await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
    assert 38 * 60 < caught.value.retry_after_seconds <= 40 * 60


async def test_adapter_quota_read_never_starts_the_engine():
    """MH-QUOTA-011: With the engine stopped, or for a vendor without a report, the read fails without starting anything."""

    adapter, _client = _adapter({}, _METADATA)
    adapter.supervisor = _Supervisor(None)
    with pytest.raises(SubscriptionQuotaError) as caught:
        await adapter.subscription_quota("src_claude", "anthropic", "cred_a")
    assert caught.value.reason == "unavailable"
    with pytest.raises(SubscriptionQuotaError) as caught:
        await adapter.subscription_quota("src_kimi", "kimi", "cred_a")
    assert caught.value.reason == "unsupported"
