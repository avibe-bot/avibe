from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_poc.environment import ProviderSettings
from memory_poc.metrics import CallMetrics
from memory_poc.pricing import estimate_ingestion_cost


def _settings(tmp_path: Path) -> ProviderSettings:
    return ProviderSettings(
        llm_base_url="configured",
        llm_model="qwen3.7-plus",
        llm_api_key="not-a-real-key",
        embedding_base_url="configured",
        embedding_model="qwen3.7-text-embedding",
        embedding_api_key="also-not-a-real-key",
        source=tmp_path / ".env.poc",
    )


def test_dashscope_china_pricing_estimate_uses_observed_ingestion_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "memory_poc.pricing.urlparse",
        lambda _value: SimpleNamespace(hostname="dashscope.aliyuncs.com"),
    )
    metrics = CallMetrics(
        ingestion_llm_calls=1,
        ingestion_embedding_calls=1,
        ingestion_llm_input_tokens=1_000,
        ingestion_llm_output_tokens=500,
        ingestion_embedding_input_tokens=2_000,
        ingestion_llm_usage_records=1,
        ingestion_embedding_usage_records=1,
    )

    estimate = estimate_ingestion_cost(_settings(tmp_path), metrics, message_count=2)

    assert estimate is not None
    assert estimate.total_cny == Decimal("0.007")
    assert estimate.per_message_cny == Decimal("0.0035")
    assert "2026-07-22" in estimate.assumption


def test_pricing_estimate_requires_observed_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "memory_poc.pricing.urlparse",
        lambda _value: SimpleNamespace(hostname="dashscope.aliyuncs.com"),
    )

    assert estimate_ingestion_cost(_settings(tmp_path), CallMetrics(), message_count=1) is None
