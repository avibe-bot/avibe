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


_TEXT_ONLY_DECLARATIONS = (["text"], [" TEXT "], ["text", "pdf"], ["Text", "audio"])
_VETO_DECLARATIONS = (
    ["text", "image"], ["text", "Image"], ["text", " image "], ["image"], ["pdf"], [],
    ["text", 1], "text", None, {"input": ["text"]},
)


def _copy(declared: object = ("text",), **fields: object) -> dict:
    return {**fields, "modalities": {"input": list(declared) if isinstance(declared, tuple) else declared}}


@pytest.mark.parametrize(
    ("relay_copies", "text_only"),
    [
        ([_copy(["text", "pdf"])], True),
        ([_copy([" TEXT "])], True),
        ([], True),
        ([_copy(["text", "image"])], False),
        ([_copy(["text", "Image"])], False),
        ([_copy(["text", " image "])], False),
        # The picker drops a copy with an empty name; the deployment it
        # describes still accepts images.
        ([_copy(["text", "image"], name="")], False),
        # A copy that is silent about input, or unreadable, is a veto.
        ([{"name": "No modalities"}], False),
        ([{"modalities": "text"}], False),
        ([_copy([])], False),
        ([_copy(["text", 1])], False),
        ([_copy("text")], False),
        (["not-an-object"], False),
        ([_copy(["image"])], False),
        ([_copy(["pdf"])], False),
        ([_copy(["text"]), _copy(["text", "image"])], False),
    ],
)
def test_text_only_needs_every_raw_copy_to_declare_text_without_images(relay_copies, text_only):
    """MH-MODALITIES-004: a text-only answer hides images, so every raw copy must declare text and no image."""

    catalog = {
        "deepseek": {"models": {"deepseek-model": _copy(["text"])}},
        **{f"relay-{index}": {"models": {"deepseek-model": copy}} for index, copy in enumerate(relay_copies)},
    }

    assert models_dev_catalog.text_only_model_ids(["deepseek-model"], catalog) == (
        {"deepseek-model"} if text_only else set()
    )


@pytest.mark.parametrize(
    ("requested", "veto_key", "veto_copy"),
    [
        # No copy outranks another: a bare relay copy vetoes a full identity.
        ("deepseek/target", "target", _copy(["text", "image"])),
        ("deepseek/target", "openrouter-name", _copy(["text", "image"], id="deepseek/target")),
        # Case and whitespace variants of the same name.
        ("target", "TARGET", _copy(["text", "image"])),
        ("Vendor/Target", "target ", _copy(["text", "image"])),
        # A relay's namespaced copy vetoes a bare request, on either side.
        ("target", "zai-org/Target", _copy(["text", "image"])),
        ("accounts/fireworks/models/target", "relay/target", _copy(["text", "image"])),
        # An entry is a copy under its key even when its id is malformed.
        ("target", "target", _copy(["text", "image"], id="")),
        ("target", "target", _copy(["text", "image"], id=" ")),
        ("target", "target", _copy(["text", "image"], id=7)),
        # ...and under its id when its key names something else.
        ("target", "hosted-7", _copy(["text", "image"], id="relay/target")),
    ],
)
def test_text_only_counts_every_entry_that_could_name_the_id_as_a_copy(requested, veto_key, veto_copy):
    """MH-MODALITIES-004: an entry is a copy of every id its key or id could name, so any doubt is a veto."""

    text_only = {"deepseek": {"models": {"target": _copy(["text"])}}}
    assert models_dev_catalog.text_only_model_ids([requested], text_only) == {requested}

    vetoed = {**text_only, "relay": {"models": {veto_key: veto_copy}}}
    assert models_dev_catalog.text_only_model_ids([requested], vetoed) == set()


def test_text_only_declares_nothing_from_a_catalog_it_cannot_read_whole(caplog):
    """MH-MODALITIES-004: a provider whose models cannot be read may hold a copy, so it vetoes every id."""

    catalog = {"deepseek": {"models": {"target": _copy(["text"])}}, "relay": {"models": [_copy(["text"])]}}

    with caplog.at_level("WARNING", logger=models_dev_catalog.__name__):
        assert models_dev_catalog.text_only_model_ids(["target", "missing"], catalog) == set()

    # The whole feature is off, so the one provider responsible is named once.
    [record] = [record for record in caplog.records if record.name == models_dev_catalog.__name__]
    assert record.levelname == "WARNING"
    assert "relay" in record.getMessage() and "deepseek" not in record.getMessage()


def _name_variants(model_id: str) -> list[str]:
    """Spellings a catalog entry may use for ``model_id``: the spec's association, written out."""

    last = model_id.rsplit("/", 1)[-1]
    return [
        model_id, last, last.upper(), f" {last} ", f"relay/{last}", f"Org/Sub/{last.upper()}",
        f"{model_id.upper()} ",
    ]


def _random_entry(rng, names: list[str]) -> object:
    if rng.random() < 0.1:
        return rng.choice(["not-an-object", 3, None, []])
    entry: dict = {}
    if rng.random() < 0.5:
        entry["id"] = rng.choice([*names, "", " ", 5, None])
    if rng.random() < 0.3:
        entry["name"] = rng.choice(["", "Name"])
    roll = rng.random()
    if roll < 0.5:
        entry["modalities"] = {"input": rng.choice(_TEXT_ONLY_DECLARATIONS)}
    elif roll < 0.9:
        entry["modalities"] = {"input": rng.choice(_VETO_DECLARATIONS)}
    elif roll < 0.95:
        entry["modalities"] = rng.choice(["text", None, {}])
    return entry


def _random_catalog(rng, requested: list[str], names: list[str]) -> dict:
    catalog: dict = {}
    # Every requested id starts with at least one copy.
    for index, model_id in enumerate(requested):
        catalog[f"owner-{index}"] = {"models": {model_id: {
            "modalities": {"input": rng.choice(_TEXT_ONLY_DECLARATIONS if rng.random() < 0.8 else _VETO_DECLARATIONS)},
        }}}
    for index in range(rng.randrange(4)):
        catalog[f"noise-{index}"] = {"models": {
            rng.choice(names): _random_entry(rng, names) for _ in range(rng.randrange(1, 4))
        }}
    return catalog


def _random_case(seed: int):
    import random

    rng = random.Random(seed)
    stems = ["target", "Beta.2", "gamma-mini", "delta"]
    requested = list(dict.fromkeys(
        rng.choice([stem, f"vendor/{stem}", f"accounts/fw/models/{stem}"]) for stem in rng.sample(stems, 3)
    ))
    names = [name for stem in stems for name in _name_variants(stem)] + ["unrelated", "vendor/unrelated"]
    return rng, requested, names


@pytest.mark.parametrize("seed", range(100))
def test_text_only_marks_never_grow_as_entries_are_added(seed):
    """MH-MODALITIES-004: for an id that has a copy, adding any entry keeps or removes its mark, never adds one."""

    import copy

    rng, requested, names = _random_case(seed)
    for trial in range(25):
        catalog = _random_catalog(rng, requested, names)
        marked = models_dev_catalog.text_only_model_ids(requested, catalog)
        grown = copy.deepcopy(catalog)
        if rng.random() < 0.05:
            grown["broken"] = {"models": [rng.choice(names)]}
        else:
            # A provider may spell part of a full identity, by key or by id.
            provider_key = rng.choice(["vendor", "Vendor", "accounts", "relay", f"added-{trial}"])
            provider = grown.setdefault(provider_key, {"models": {}})
            if rng.random() < 0.3:
                provider["id"] = rng.choice(["vendor", "accounts/fw/models"])
            key = rng.choice([name for name in names if name not in provider["models"]])
            provider["models"][key] = _random_entry(rng, names)
        assert models_dev_catalog.text_only_model_ids(requested, grown) <= marked, (seed, catalog, grown)


@pytest.mark.parametrize("seed", range(100))
def test_text_only_marks_fall_to_a_veto_under_any_spelling_of_the_id(seed):
    """MH-MODALITIES-004: a vetoing copy under any spelling that could name a marked id removes its mark."""

    rng, requested, names = _random_case(seed)
    for _trial in range(25):
        catalog = _random_catalog(rng, requested, names)
        for model_id in sorted(models_dev_catalog.text_only_model_ids(requested, catalog)):
            spelling = rng.choice(_name_variants(model_id))
            veto: object = {"modalities": {"input": rng.choice(_VETO_DECLARATIONS)}}
            roll = rng.random()
            if roll < 0.4:
                key, veto = rng.choice(["hosted", *names]), {**veto, "id": spelling}
            elif roll < 0.9:
                key = spelling
                if rng.random() < 0.3:
                    veto["id"] = rng.choice(["", " ", 5])
                if rng.random() < 0.2:
                    veto = rng.choice(["not-an-object", None, {"name": spelling}])
            else:
                vetoed = {**catalog, "broken": {"models": [spelling]}}
                assert model_id not in models_dev_catalog.text_only_model_ids(requested, vetoed), (seed, model_id)
                continue
            if isinstance(veto, dict) and rng.random() < 0.3:
                veto["name"] = ""
            vetoed = {**catalog, "veto": {"models": {key: veto}}}
            assert model_id not in models_dev_catalog.text_only_model_ids(requested, vetoed), (
                seed, model_id, key, veto,
            )


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
