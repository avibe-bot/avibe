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
                        {
                            "type": "effort",
                            "values": ["low", "x" * 65, "high", "low"],
                        }
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
            "first_party": True,
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
            "native_protocol": "openai_responses",
        }
    ]
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["etag"] == '"catalog-v1"'
    assert cached["catalog"] == _catalog()


def test_models_dev_search_preserves_validator_accepted_long_names(monkeypatch):
    model_id = "m" * 80
    display_name = "Model " + "x" * 80
    monkeypatch.setattr(
        models_dev_catalog,
        "load_models_dev_catalog",
        lambda: {
            "provider": {
                "name": "Provider",
                "models": {model_id: {"name": display_name}},
            }
        },
    )

    match = models_dev_catalog.search_models_dev(model_id)[0]

    assert match["model_id"] == model_id
    assert match["display_name"] == display_name


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

    assert models_dev_catalog.search_models_dev("deepseek/deepseek-v4")[0]["models_dev_id"] == "deepseek/deepseek-v4"


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

    assert models_dev_catalog.search_models_dev("DeepSeek V4")[0]["context_window"] == 1_048_576


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


def test_model_vendor_map_is_versioned_unambiguous_and_ordered():
    vendor_map = models_dev_catalog.load_model_vendor_map()
    families = vendor_map["families"]
    aggregators = vendor_map["aggregators"]

    assert vendor_map["schema_version"] == 1
    assert families
    assert all(set(item) == {"prefix", "vendor_id"} and item["prefix"] and item["vendor_id"] for item in families)
    assert len({item["prefix"] for item in families}) == len(families)
    assert aggregators
    assert len(set(aggregators)) == len(aggregators)
    assert all(isinstance(provider_id, str) and provider_id for provider_id in aggregators)


def test_native_protocol_derivation_uses_every_vendor_family_on_the_last_segment():
    vendor_map = models_dev_catalog.load_model_vendor_map()

    for family in vendor_map["families"]:
        model_id = f"aggregator/nested/{family['prefix']}fixture"
        expected = (
            "anthropic"
            if family["vendor_id"] == "anthropic"
            else "openai_responses"
        )
        assert models_dev_catalog.native_protocol_for_model_id(
            model_id,
            vendor_map=vendor_map,
        ) == expected


def test_models_dev_deduplicates_by_model_id_and_ranks_first_party_then_match(
    monkeypatch,
):
    catalog = {
        "openrouter": {
            "name": "OpenRouter",
            "models": {
                "gpt-target": {"name": "Target proxy"},
                "target": {"name": "Target"},
            },
        },
        "openai": {
            "name": "OpenAI",
            "models": {"gpt-target": {"name": "GPT target"}},
        },
        "zeta": {
            "name": "Zeta",
            "models": {"target": {"name": "Target"}},
        },
    }
    monkeypatch.setattr(
        models_dev_catalog,
        "load_models_dev_catalog",
        lambda: catalog,
    )

    matches = models_dev_catalog.search_models_dev("target")

    assert [item["model_id"] for item in matches] == ["gpt-target", "target"]
    assert matches[0]["provider_id"] == "openai"
    assert matches[0]["first_party"] is True
    assert matches[1]["provider_id"] == "openrouter"
    assert matches[1]["first_party"] is False


def test_models_dev_openai_o_series_does_not_claim_other_o_families(monkeypatch):
    catalog = {
        "openrouter": {
            "name": "OpenRouter",
            "models": {
                "o3-target": {"name": "O3 proxy"},
                "olmo-target": {"name": "OLMo proxy"},
            },
        },
        "openai": {
            "name": "OpenAI",
            "models": {
                "o3-target": {"name": "O3 target"},
                "olmo-target": {"name": "OLMo OpenAI copy"},
            },
        },
    }
    monkeypatch.setattr(
        models_dev_catalog,
        "load_models_dev_catalog",
        lambda: catalog,
    )

    matches = {
        item["model_id"]: item for item in models_dev_catalog.search_models_dev("target")
    }

    assert matches["o3-target"]["provider_id"] == "openai"
    assert matches["o3-target"]["first_party"] is True
    assert matches["olmo-target"]["provider_id"] == "openrouter"
    assert matches["olmo-target"]["first_party"] is False


def test_models_dev_normalizes_each_copy_before_ranking_and_deduplication(
    monkeypatch,
):
    catalog = {
        "openai": {
            "name": "OpenAI",
            "models": {
                "gpt-target": {"name": "   "},
                "gpt-unusable-target": {
                    "name": "Unusable target",
                    "reasoning_options": [
                        {"type": "effort", "values": ["   "]}
                    ],
                },
            },
        },
        "openrouter": {
            "name": "OpenRouter",
            "models": {"gpt-target": {"name": "Valid aggregator copy"}},
        },
    }
    monkeypatch.setattr(
        models_dev_catalog,
        "load_models_dev_catalog",
        lambda: catalog,
    )

    matches = models_dev_catalog.search_models_dev("target")

    assert [item["model_id"] for item in matches] == [
        "gpt-unusable-target",
        "gpt-target",
    ]
    assert matches[0]["reasoning_efforts"] == []
    assert matches[1]["provider_id"] == "openrouter"
    assert matches[1]["first_party"] is False


def test_models_dev_caps_matches_at_eight(monkeypatch):
    catalog = {
        "unknown": {
            "name": "Unknown",
            "models": {f"test-{index:02d}": {"name": f"Test {index:02d}"} for index in range(12)},
        }
    }
    monkeypatch.setattr(
        models_dev_catalog,
        "load_models_dev_catalog",
        lambda: catalog,
    )

    matches = models_dev_catalog.search_models_dev("test")

    assert len(matches) == models_dev_catalog.MODELS_DEV_MAX_MATCHES == 8
    assert [item["model_id"] for item in matches] == sorted(item["model_id"] for item in matches)


def test_exact_models_dev_matches_never_borrow_a_neighbour():
    catalog = {
        "openrouter": {
            "name": "OpenRouter",
            "models": {
                "gpt-target": {"name": "Proxy"},
                "claude-3-5-target": {"name": "Claude 3.5 proxy"},
            },
        },
        "openai": {
            "name": "OpenAI",
            "models": {
                "gpt-target": {"name": "GPT target"},
                "gpt-target-mini": {"name": "GPT target mini"},
                "foo": {"name": "Foo"},
                "Case-Target": {"name": "Case target"},
            },
        },
    }

    matches = models_dev_catalog.exact_models_dev_matches(
        [
            "gpt-target", "openrouter/gpt-target", "relay/gpt-target-mini",
            "open_router/gpt.target", "claude-3.5-target", "gpt", "gpt-target-max",
            "avibe-foo", "case-target",
        ],
        catalog,
    )

    # A bare id prefers the first-party copy; a full identity names its own.
    assert matches["gpt-target"]["models_dev_id"] == "openai/gpt-target"
    assert matches["openrouter/gpt-target"]["models_dev_id"] == "openrouter/gpt-target"
    # A relay prefix falls back to the last segment.
    assert matches["relay/gpt-target-mini"]["models_dev_id"] == "openai/gpt-target-mini"
    # Only identical spellings match: no punctuation, case, or alias folding and
    # no substrings.
    assert set(matches) == {"gpt-target", "openrouter/gpt-target", "relay/gpt-target-mini"}
    assert models_dev_catalog.exact_models_dev_matches(["gpt-target"], {}) == {}


def _entry(declared: object, **fields: object) -> dict:
    return {**fields, "modalities": {"input": declared}}


@pytest.mark.parametrize(
    ("entries", "requested", "text_only"),
    [
        ({"target": _entry(["text"])}, "target", True),
        ({"target": _entry([" TEXT ", "pdf"])}, "target", True),
        ({"target": _entry(["text", "image"])}, "target", False),
        ({"target": _entry(["text", " Image "])}, "target", False),
        # Undeclared or malformed input leaves the model undeclared.
        ({"target": {"name": "No modalities"}}, "target", False),
        ({"target": _entry([])}, "target", False),
        ({"target": _entry(["text", 1])}, "target", False),
        ({"target": _entry("text")}, "target", False),
        ({"target": "not-an-object"}, "target", False),
        ({"target": _entry(["image"])}, "target", False),
        # Model identity is Avibe's: trimmed, case preserved.
        ({"target": _entry(["text"])}, " target ", True),
        ({"target": _entry(["text"])}, "TARGET", False),
        ({"TARGET": _entry(["text", "image"]), "target": _entry(["text"])}, "TARGET", False),
        # An entry is found by its key or its id; one that disagrees vetoes.
        ({"hosted-7": _entry(["text"], id="target")}, "target", True),
        ({"target": _entry(["text"]), "alias": _entry(["text", "image"], id="target")}, "target", False),
        ({"other": _entry(["text"])}, "target", False),
    ],
)
def test_text_only_reads_the_upstream_providers_own_entry(entries, requested, text_only):
    """MH-MODALITIES-004: a model is text-only when its upstream provider's own entry declares text and no image."""

    catalog = {"deepseek": {"models": entries}}

    assert models_dev_catalog.text_only_model_ids("deepseek", [requested], catalog) == (
        {requested} if text_only else set()
    )


def test_text_only_never_reads_another_providers_copy():
    """MH-MODALITIES-004: a relay's declaration describes the relay, so it neither marks nor vetoes the vendor."""

    catalog = {
        "zhipuai": {"models": {"glm-5.2": _entry(["text"])}},
        "baseten": {"models": {"zai-org/GLM-5.2": _entry(["text", "image"])}},
        "relay": {"models": {"deepseek-model": _entry(["text"])}},
        "deepseek": {"models": {}},
    }

    assert models_dev_catalog.text_only_model_ids("zhipuai", ["glm-5.2"], catalog) == {"glm-5.2"}
    assert models_dev_catalog.text_only_model_ids("deepseek", ["deepseek-model"], catalog) == set()


def test_text_only_warns_about_an_unreadable_provider_without_touching_others(caplog):
    """MH-MODALITIES-004: an unreadable or missing provider declares nothing and says so; other providers are unaffected."""

    catalog = {
        "deepseek": {"models": [_entry(["text"])]},
        "zhipuai": {"models": {"glm-5": _entry(["text"])}},
    }

    with caplog.at_level("WARNING", logger=models_dev_catalog.__name__):
        assert models_dev_catalog.text_only_model_ids("deepseek", ["deepseek-model"], catalog) == set()
        assert models_dev_catalog.text_only_model_ids("zhipuai", ["glm-5"], catalog) == {"glm-5"}
        assert models_dev_catalog.text_only_model_ids("groq", ["model"], catalog) == set()
        # No cached copy yet is not a fault.
        assert models_dev_catalog.text_only_model_ids("groq", ["model"], {}) == set()

    messages = [record.getMessage() for record in caplog.records if record.name == models_dev_catalog.__name__]
    assert len(messages) == 2
    assert "deepseek" in messages[0] and "zhipuai" not in messages[0]
    assert "groq" in messages[1]


def test_cached_catalog_read_does_not_wait_on_a_foreground_fetch(monkeypatch, tmp_path):
    """MH-MODALITIES-002: an engine sync reads the cached copy while a picker fetch holds the cache lock."""

    import threading

    monkeypatch.setattr(models_dev_catalog, "_cache_path", lambda: tmp_path / "models_dev_catalog.json")
    models_dev_catalog._write_cache({"url": models_dev_catalog._models_dev_url(), "fetched_at": 0, "catalog": _catalog()})
    read: list[dict] = []
    with models_dev_catalog._CACHE_LOCK:
        reader = threading.Thread(target=lambda: read.append(models_dev_catalog.cached_models_dev_catalog()))
        reader.start()
        reader.join(timeout=5)
        waited = reader.is_alive()
    reader.join(timeout=5)

    assert not waited
    assert read == [_catalog()]


def test_first_catalog_read_reports_one_fetch_in_flight_until_it_fails(monkeypatch, tmp_path):
    """MH-PRICE-014: With no cached copy, readers start one fetch and say one is coming; after a failure they do not."""

    import threading

    monkeypatch.setattr(models_dev_catalog, "_cache_path", lambda: tmp_path / "models_dev_catalog.json")
    monkeypatch.setattr(models_dev_catalog, "_refresh_in_flight", False, raising=False)
    monkeypatch.setattr(models_dev_catalog, "_last_refresh_failed", False, raising=False)
    release = threading.Event()
    fetches = []

    def fetch(_previous):
        fetches.append(threading.current_thread().name)
        release.wait(timeout=5)
        raise OSError("offline")

    monkeypatch.setattr(models_dev_catalog, "_fetch_catalog", fetch)
    readers = [threading.Thread(target=models_dev_catalog.load_models_dev_catalog_with_date) for _ in range(8)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(timeout=5)
    assert models_dev_catalog.load_models_dev_catalog_with_date() == ({}, None, True)
    release.set()
    for thread in threading.enumerate():
        if thread.name == "models-dev-refresh":
            thread.join(timeout=5)
    assert fetches == ["models-dev-refresh"]
    # The fetch failed: the next read retries it, but no longer promises prices.
    assert models_dev_catalog.load_models_dev_catalog_with_date() == ({}, None, False)
