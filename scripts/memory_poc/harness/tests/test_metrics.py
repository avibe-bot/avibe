from __future__ import annotations

from pathlib import Path

from memory_poc.metrics import append_egress_metric, append_request_metric, classify_request_path, read_call_metrics, read_egress_hosts


def test_metric_log_contains_only_counts_and_tokens(tmp_path: Path) -> None:
    path = tmp_path / "request-counts.jsonl"
    append_request_metric(
        path,
        kind="llm",
        usage={"prompt_tokens": 7, "completion_tokens": 3, "content": "ignored"},
        phase="ingestion",
    )
    append_request_metric(path, kind="embedding", usage={"input_tokens": 11}, phase="read")

    metrics = read_call_metrics(path)

    assert metrics.llm_calls == 1
    assert metrics.embedding_calls == 1
    assert metrics.llm_input_tokens == 7
    assert metrics.llm_output_tokens == 3
    assert metrics.embedding_input_tokens == 11
    assert metrics.ingestion_llm_calls == 1
    assert metrics.ingestion_embedding_calls == 0
    assert metrics.ingestion_llm_input_tokens == 7
    assert metrics.ingestion_llm_output_tokens == 3
    assert metrics.ingestion_llm_usage_records == 1
    assert "content" not in path.read_text(encoding="utf-8")
    assert classify_request_path("/v1/embeddings") == "embedding"


def test_egress_log_keeps_only_unique_hostname_values(tmp_path: Path) -> None:
    path = tmp_path / "egress.jsonl"

    append_egress_metric(path, hostname="DashScope.AliYuncs.com")
    append_egress_metric(path, hostname="127.0.0.1")
    append_egress_metric(path, hostname="https://not-a-host.invalid/path")
    append_egress_metric(path, hostname="dashscope.aliyuncs.com")

    assert read_egress_hosts(path) == ("dashscope.aliyuncs.com",)
    rendered = path.read_text(encoding="utf-8")
    assert "https://" not in rendered
    assert "127.0.0.1" not in rendered
