from __future__ import annotations

import json
import time
import urllib.error

import pytest

from vibe import models_dev_catalog


class _Response:
    def __init__(self, payload, *, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps(self.payload).encode()


def _catalog():
    return {
        "deepseek": {
            "id": "deepseek",
            "name": "DeepSeek",
            "models": {
                "deepseek-v4": {
                    "id": "deepseek-v4",
                    "name": "DeepSeek V4",
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high", "low"]}
                    ],
                    "tool_call": True,
                    "modalities": {
                        "input": ["text", "image", "image", "pdf", "unknown"],
                        "output": ["text", "text", "pdf"],
                    },
                    "limit": {"context": 1_048_576, "output": 131_072},
                }
            },
        }
    }


def test_models_dev_search_normalizes_editable_metadata(monkeypatch, tmp_path):
    cache_path = tmp_path / "models-dev.json"
    monkeypatch.setattr(models_dev_catalog, "_cache_path", lambda: cache_path)
    monkeypatch.setattr(
        models_dev_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(
            _catalog(),
            headers={"ETag": '"catalog-v1"'},
        ),
    )

    matches = models_dev_catalog.search_models_dev("deepseek-v4")

    assert matches == [
        {
            "provider_id": "deepseek",
            "provider_name": "DeepSeek",
            "model_id": "deepseek-v4",
            "models_dev_id": "deepseek/deepseek-v4",
            "display_name": "DeepSeek V4",
            "context_window": 1_048_576,
            "max_output_tokens": 131_072,
            "input_modalities": ["text", "image", "pdf"],
            "output_modalities": ["text"],
            "supports_tools": True,
            "supports_reasoning": True,
            "reasoning_efforts": ["low", "high"],
        }
    ]
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["etag"] == '"catalog-v1"'
    assert cached["catalog"] == _catalog()


def test_models_dev_search_uses_fresh_cache_without_network(monkeypatch, tmp_path):
    cache_path = tmp_path / "models-dev.json"
    monkeypatch.setattr(models_dev_catalog, "_cache_path", lambda: cache_path)
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": time.time(),
                "url": models_dev_catalog.DEFAULT_MODELS_DEV_URL,
                "catalog": _catalog(),
            }
        ),
        encoding="utf-8",
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("fresh cache must avoid network")

    monkeypatch.setattr(models_dev_catalog.urllib.request, "urlopen", unexpected)

    assert models_dev_catalog.search_models_dev("deepseek/deepseek-v4")[0][
        "models_dev_id"
    ] == "deepseek/deepseek-v4"


def test_models_dev_search_falls_back_to_stale_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "models-dev.json"
    monkeypatch.setattr(models_dev_catalog, "_cache_path", lambda: cache_path)
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": 0,
                "url": models_dev_catalog.DEFAULT_MODELS_DEV_URL,
                "catalog": _catalog(),
            }
        ),
        encoding="utf-8",
    )

    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(models_dev_catalog.urllib.request, "urlopen", unavailable)

    assert models_dev_catalog.search_models_dev("DeepSeek V4")[0][
        "context_window"
    ] == 1_048_576


def test_models_dev_stale_cache_is_scoped_to_its_url(monkeypatch, tmp_path):
    cache_path = tmp_path / "models-dev.json"
    monkeypatch.setattr(models_dev_catalog, "_cache_path", lambda: cache_path)
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": 0,
                "url": models_dev_catalog.DEFAULT_MODELS_DEV_URL,
                "catalog": _catalog(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        models_dev_catalog.MODELS_DEV_URL_ENV,
        "https://catalog.example.invalid/api.json",
    )

    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(models_dev_catalog.urllib.request, "urlopen", unavailable)

    with pytest.raises(RuntimeError, match="unavailable"):
        models_dev_catalog.search_models_dev("deepseek-v4")
