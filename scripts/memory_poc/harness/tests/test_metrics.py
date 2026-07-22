from __future__ import annotations

from pathlib import Path

from memory_poc.metrics import append_request_metric, classify_request_path, read_call_metrics


def test_metric_log_contains_only_counts_and_tokens(tmp_path: Path) -> None:
    path = tmp_path / "request-counts.jsonl"
    append_request_metric(path, kind="llm", usage={"prompt_tokens": 7, "completion_tokens": 3, "content": "ignored"})
    append_request_metric(path, kind="embedding", usage={"input_tokens": 11})

    metrics = read_call_metrics(path)

    assert metrics.llm_calls == 1
    assert metrics.embedding_calls == 1
    assert metrics.llm_input_tokens == 7
    assert metrics.llm_output_tokens == 3
    assert metrics.embedding_input_tokens == 11
    assert "content" not in path.read_text(encoding="utf-8")
    assert classify_request_path("/v1/embeddings") == "embedding"
