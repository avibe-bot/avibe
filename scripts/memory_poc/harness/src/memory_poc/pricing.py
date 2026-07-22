from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from .environment import ProviderSettings
from .metrics import CallMetrics

_DASHSCOPE_CHINA_HOST = "dashscope.aliyuncs.com"
_LLM_MODEL = "qwen3.7-plus"
_EMBEDDING_MODEL = "qwen3.7-text-embedding"
_TOKENS_PER_MILLION = Decimal(1_000_000)
_LLM_INPUT_CNY_PER_MILLION = Decimal("2")
_LLM_OUTPUT_CNY_PER_MILLION = Decimal("8")
_EMBEDDING_INPUT_CNY_PER_MILLION = Decimal("0.5")


@dataclass(frozen=True)
class CostEstimate:
    total_cny: Decimal
    per_message_cny: Decimal
    assumption: str


def estimate_ingestion_cost(settings: ProviderSettings, metrics: CallMetrics, *, message_count: int) -> CostEstimate | None:
    """Estimate only the configured DashScope China list-rate case with observed usage."""
    if message_count < 1 or not _is_supported_dashscope_configuration(settings):
        return None
    if not (metrics.ingestion_llm_calls or metrics.ingestion_embedding_calls):
        return None
    if (
        metrics.ingestion_llm_usage_records != metrics.ingestion_llm_calls
        or metrics.ingestion_embedding_usage_records != metrics.ingestion_embedding_calls
    ):
        return None
    total = (
        Decimal(metrics.ingestion_llm_input_tokens) * _LLM_INPUT_CNY_PER_MILLION
        + Decimal(metrics.ingestion_llm_output_tokens) * _LLM_OUTPUT_CNY_PER_MILLION
        + Decimal(metrics.ingestion_embedding_input_tokens) * _EMBEDDING_INPUT_CNY_PER_MILLION
    ) / _TOKENS_PER_MILLION
    return CostEstimate(
        total_cny=total,
        per_message_cny=total / Decimal(message_count),
        assumption=(
            "DashScope China list-rate snapshot 2026-07-22; qwen3.7-plus <=256K, "
            "qwen3.7-text-embedding, no cache or batch discount"
        ),
    )


def _is_supported_dashscope_configuration(settings: ProviderSettings) -> bool:
    return (
        settings.llm_model == _LLM_MODEL
        and settings.embedding_model == _EMBEDDING_MODEL
        and (urlparse(settings.llm_base_url).hostname or "").lower() == _DASHSCOPE_CHINA_HOST
        and (urlparse(settings.embedding_base_url).hostname or "").lower() == _DASHSCOPE_CHINA_HOST
    )
