"""The overlay's ``providers`` section (``model-hub-capability-model.md`` §4).

The loader is the one reader of the bundled section, so its resolution and its
degradation are pinned here, with the JSON Schema held to the same verdicts.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource

from vibe import backend_model_catalog as catalog
from vibe.backend_model_catalog import HostedToolPrice, HostedToolPriceComponent

CONTRACTS = Path(__file__).parents[1] / "docs/plans/model-hub-contracts"
BUNDLED = json.loads(catalog.get_bundled_catalog_path().read_text(encoding="utf-8"))
BUNDLED_ROWS = catalog.load_bundled_catalog()["backends"]
AS_OF = date(2026, 10, 13)
_DROP = object()
OPENAI_WEB_SEARCH = (
    {"type": "web_search"},
    ("web_search_call.action.sources",),
    HostedToolPrice("call", 10.0, AS_OF, "https://developers.openai.com/api/docs/pricing"),
)


@pytest.fixture(autouse=True)
def _fresh_overlay():
    catalog.bundled_provider_vendors.cache_clear()
    yield
    catalog.bundled_provider_vendors.cache_clear()


def _validator() -> Draft7Validator:
    schemas = [
        json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
        for name in (
            "provider-overlay.schema.json",
            "source.schema.json",
            "source-probe-result.schema.json",
        )
    ]
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    return Draft7Validator(schemas[0], registry=registry, format_checker=FormatChecker())


def _use_bundled(monkeypatch, tmp_path: Path, providers: object) -> None:
    path = tmp_path / "backend_models.json"
    path.write_text(json.dumps({**BUNDLED, "providers": providers}), encoding="utf-8")
    monkeypatch.setattr(catalog, "get_bundled_catalog_path", lambda *_args, **_kwargs: path)


def _overlay_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == catalog.__name__ and record.levelno == logging.WARNING
    ]


def test_bundled_providers_section_satisfies_its_schema():
    _validator().validate(BUNDLED["providers"])


@pytest.mark.parametrize(
    ("vendor", "protocol", "model", "tool", "expected"),
    [
        ("openai", "openai_responses", "gpt-6-astra", "web_search", OPENAI_WEB_SEARCH),
        ("openai", "openai_responses", " gpt-6-astra ", "web_search", OPENAI_WEB_SEARCH),
        # Image generation is never a chat declaration; image models are listed apart.
        ("openai", "openai_responses", "gpt-6-astra", "image_generation", None),
        (
            "xai",
            "openai_responses",
            "grok-4.6",
            "x_search",
            (
                {"type": "x_search"},
                (),
                HostedToolPrice(
                    "post",
                    5.0,
                    AS_OF,
                    "https://docs.x.ai/developers/pricing",
                    extra=(HostedToolPriceComponent("profile", 10.0),),
                ),
            ),
        ),
        (
            "anthropic",
            "anthropic",
            "claude-opus-5-5",
            "web_search",
            (
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
                (),
                HostedToolPrice("search", 10.0, AS_OF, "https://platform.claude.com/docs/en/about-claude/pricing"),
            ),
        ),
        ("mistral", "openai_responses", "gpt-6-astra", "web_search", None),
        ("openai", "openai_responses", "o3", "web_search", None),
        ("xai", "openai_responses", "grok-3", "web_search", None),
        ("xai", "anthropic", "grok-4.6", "web_search", None),
        # A custom relay names no vendor: a model takes the priors of the vendor that claims it (#2873) ...
        ("custom", "openai_responses", "gpt-6-luna", "web_search", OPENAI_WEB_SEARCH),
        # ... but only on that vendor's own protocol, the one its declarations were verified on,
        ("custom", "openai_chat", "gpt-6-luna", "web_search", None),
        ("custom", "openai_responses", "claude-opus-5-5", "web_search", None),
        # and a model no vendor claims takes none.
        ("custom", "openai_responses", "deepseek-chat", "web_search", None),
    ],
)
def test_bundled_overlay_resolves_hosted_tools(vendor, protocol, model, tool, expected):
    """MH-HOSTED-005: the bundled overlay resolves each hosted tool declaration, include and price."""
    spec = catalog.hosted_tool_spec(vendor, protocol, model, tool)

    if expected is None:
        assert spec is None
        return
    assert (spec.declaration, spec.include, spec.price) == expected
    spec.declaration["type"] = "changed"
    assert catalog.hosted_tool_spec(vendor, protocol, model, tool).declaration == expected[0]


@pytest.mark.parametrize(
    ("model", "wire_efforts", "has_web_search"),
    [
        ("gpt-6-astra", ("low", "high"), False),
        ("gpt-6-astra-mini", None, True),
        ("gpt-5.5", ("minimal",), False),
        ("gpt", None, None),
    ],
)
def test_overlay_families_match_in_order_and_the_first_match_wins(
    monkeypatch, tmp_path, model, wire_efforts, has_web_search
):
    web_search = {"openai_responses": {"web_search": {"declaration": {"type": "web_search"}}}}
    _use_bundled(
        monkeypatch,
        tmp_path,
        {
            "openai": {
                "models": [
                    {"match": {"id": "gpt-6-astra"}, "wire_efforts": ["low", "high"]},
                    {"match": {"prefix": "gpt-6"}, "wire_efforts": None, "hosted_tools": web_search},
                    {"match": {"prefix": "gpt-"}, "wire_efforts": ["minimal"]},
                ]
            }
        },
    )

    entry = catalog.provider_overlay_entry("openai", model)
    spec = catalog.hosted_tool_spec("openai", "openai_responses", model, "web_search")

    if has_web_search is None:
        assert entry is None and spec is None
        return
    assert entry.wire_efforts == wire_efforts
    assert (spec is not None) is has_web_search


def _edit(path: tuple, value: object):
    def invalid(entry: dict) -> dict:
        entry = copy.deepcopy(entry)
        *parents, leaf = path
        node = entry
        for key in parents:
            node = node[key]
        if value is _DROP:
            del node[leaf]
        else:
            node[leaf] = value
        return entry

    return invalid


def _with_image(entry: dict) -> dict:
    return {**entry, "image_models": [copy.deepcopy(_IMAGE_MODEL)]}


def _image_edit(path: tuple, value: object):
    edit = _edit(("image_models", 0, *path), value)
    return lambda entry: edit(_with_image(entry))


_IMAGE_MODEL = {
    "id": "grok-imagine-image",
    "description": "Grok Imagine: fast photoreal images",
    "params": ["size", "images"],
    "size_forms": ["ratio"],
    "kinds": ["api_key"],
    "request": {"response_format": "b64_json"},
    "prices": [{"price": {"unit": "image", "usd_per_1k": 20, "as_of": "2026-10-15", "url": "https://docs.x.ai/developers/pricing"}}],
}
_FAMILY = ("models", 0)
_X_SEARCH = (*_FAMILY, "hosted_tools", "openai_responses", "x_search")
_PRICE = (*_X_SEARCH, "price")


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        pytest.param("xai", lambda entry: [], id="entry-not-an-object"),
        pytest.param("xAI", lambda entry: entry, id="vendor-not-canonical"),
        pytest.param("xai", _edit(("models",), _DROP), id="models-missing"),
        pytest.param("xai", _edit(("models",), {}), id="models-not-a-list"),
        pytest.param("xai", _edit(("note",), "x"), id="unknown-vendor-key"),
        pytest.param("xai", _edit((*_FAMILY, "tier"), 1), id="unknown-family-key"),
        pytest.param("xai", _edit((*_FAMILY, "match"), _DROP), id="match-missing"),
        pytest.param("xai", _edit((*_FAMILY, "match"), {}), id="match-empty"),
        pytest.param("xai", _edit((*_FAMILY, "match", "id"), "grok-4.6"), id="match-prefix-and-id"),
        pytest.param("xai", _edit((*_FAMILY, "match"), {"prefix": " grok-4"}), id="match-padded"),
        pytest.param("xai", _edit((*_FAMILY, "wire_efforts"), "high"), id="wire-efforts-not-a-list"),
        pytest.param("xai", _edit((*_FAMILY, "wire_efforts"), ["high", "high"]), id="wire-efforts-repeated"),
        pytest.param("xai", _edit((*_FAMILY, "hosted_tools", "responses"), {}), id="unknown-protocol"),
        pytest.param(
            "xai",
            _edit(
                (*_FAMILY, "hosted_tools", "openai_responses", "code_interpreter"),
                {"declaration": {"type": "code_interpreter"}},
            ),
            id="unknown-tool",
        ),
        pytest.param("xai", _edit((*_X_SEARCH, "declaration"), _DROP), id="declaration-missing"),
        pytest.param("xai", _edit((*_X_SEARCH, "declaration"), {"name": "x_search"}), id="declaration-untyped"),
        pytest.param("xai", _edit((*_X_SEARCH, "include"), [""]), id="include-blank"),
        pytest.param("xai", _edit(_PRICE, None), id="price-null"),
        pytest.param("xai", _edit((*_PRICE, "unit"), "token"), id="price-unit-unknown"),
        # Only an image model is priced per image.
        pytest.param("xai", _edit((*_PRICE, "unit"), "image"), id="search-price-per-image"),
        pytest.param("xai", _edit((*_PRICE, "usd_per_1k"), -1), id="price-negative"),
        pytest.param("xai", _edit((*_PRICE, "usd_per_1k"), True), id="price-boolean"),
        pytest.param("xai", _edit((*_PRICE, "as_of"), "2026/10/13"), id="as-of-not-iso"),
        pytest.param("xai", _edit((*_PRICE, "as_of"), "2026-02-30"), id="as-of-impossible"),
        pytest.param("xai", _edit((*_PRICE, "url"), "http://docs.x.ai/developers/pricing"), id="url-not-https"),
        pytest.param("xai", _edit((*_PRICE, "url"), _DROP), id="url-missing"),
        pytest.param("xai", _edit((*_PRICE, "extra", 0, "unit"), "image"), id="extra-unit-per-image"),
        pytest.param("xai", _edit(("executor_models", 0), " "), id="executor-model-blank"),
        pytest.param("xai", _edit(("executor_models",), "grok-4.20-0309-non-reasoning"), id="executor-models-not-a-list"),
        pytest.param("xai", _edit(("executor_models",), []), id="executor-models-empty"),
        pytest.param(
            "xai", _edit(("executor_models",), ["grok-4.20-0309-non-reasoning"] * 2), id="executor-model-repeated",
        ),
        pytest.param("xai", _edit(("image_models",), {}), id="image-models-not-a-list"),
        pytest.param("xai", _image_edit(("kinds",), ["oauth"]), id="image-kind-unknown"),
        pytest.param("xai", _image_edit(("kinds",), []), id="image-kinds-empty"),
        # A fixed request field never overrides what the call itself names.
        pytest.param("xai", _image_edit(("request",), {"n": 4}), id="image-request-sets-a-call-field"),
        # A ratio-form size is sent as aspect_ratio: fixing it would override, or be overridden by, the call.
        pytest.param("xai", _image_edit(("request",), {"aspect_ratio": "1:1"}), id="image-request-sets-the-ratio-field"),
        pytest.param("xai", _image_edit(("request",), {"style": {"nested": True}}), id="image-request-not-scalar"),
        pytest.param("xai", _image_edit(("description",), "x" * 81), id="image-description-too-long"),
        pytest.param("xai", _image_edit(("params",), ["images", "quality"]), id="image-param-unknown"),
        pytest.param("xai", _image_edit(("size_forms",), []), id="image-size-without-forms"),
        pytest.param("xai", _image_edit(("prices", 0, "price", "unit"), "call"), id="image-price-not-per-image"),
        pytest.param("xai", _image_edit(("tokens_per_image",), 0), id="image-tokens-not-positive"),
        pytest.param(
            "xai", _image_edit(("prices", 0, "price", "extra"), [{"unit": "image", "usd_per_1k": 1}]), id="image-price-extra",
        ),
    ],
)
def test_an_invalid_vendor_entry_disables_only_that_vendor(monkeypatch, tmp_path, caplog, key, invalid):
    providers = {
        "anthropic": BUNDLED["providers"]["anthropic"],
        "openai": BUNDLED["providers"]["openai"],
        key: invalid(BUNDLED["providers"]["xai"]),
    }
    assert not _validator().is_valid(providers)
    _use_bundled(monkeypatch, tmp_path, providers)

    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        overlay = catalog.bundled_provider_overlay()

    assert set(overlay) == {"anthropic", "openai"}
    assert catalog.hosted_tool_spec("openai", "openai_responses", "gpt-6-astra", "web_search") is not None
    warnings = _overlay_warnings(caplog)
    assert len(warnings) == 1 and repr(key) in warnings[0]


def test_an_image_model_entry_is_read_in_a_stable_form(monkeypatch, tmp_path):
    """MH-HOSTED-005: an image model's params and size forms are sorted, so the tool block built from them is stable."""
    providers = {**BUNDLED["providers"], "xai": _with_image(BUNDLED["providers"]["xai"])}
    _validator().validate(providers)
    _use_bundled(monkeypatch, tmp_path, providers)

    (model,) = catalog.provider_image_models("xai")
    assert (model.id, model.params, model.size_forms, model.kinds, model.request) == (
        "grok-imagine-image", ("images", "size"), ("ratio",), ("api_key",), {"response_format": "b64_json"},
    )
    assert [(price.price.unit, price.price.usd_per_1k, price.size) for price in model.prices] == [("image", 20, None)]
    assert catalog.provider_image_models("anthropic") == ()


EXECUTOR_PROTOCOLS = {"anthropic", "openai_responses"}


@pytest.mark.parametrize("vendor", sorted(BUNDLED["providers"]))
def test_each_vendors_executor_models_run_every_search_tool_the_vendor_offers(vendor):
    """MH-HOSTED-005: an executor model the overlay does not declare a tool on would silently drop the vendor from
    that tool's executor chain on every Source that lists it first.

    The executor speaks Anthropic and Responses (``hosted_executor.executor_request``); Gemini runs Google Search
    natively on its own hop only (``vibey-gemini-native.md`` §6), so a tool offered on another protocol needs none.
    """
    offered = {
        (protocol, tool)
        for family in catalog.bundled_provider_overlay()[vendor]
        for protocol, tools in family.hosted_tools.items()
        if protocol in EXECUTOR_PROTOCOLS
        for tool in tools
    }
    executors = catalog.provider_executor_models(vendor)
    assert bool(executors) is bool(offered)
    for executor in executors:
        for protocol, tool in offered:
            assert catalog.hosted_tool_spec(vendor, protocol, executor, tool) is not None, (executor, protocol, tool)


@pytest.mark.parametrize("providers", [[], "openai", None])
def test_a_providers_value_that_is_not_an_object_disables_only_the_overlay(
    monkeypatch, tmp_path, caplog, providers
):
    _use_bundled(monkeypatch, tmp_path, providers)

    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        overlay = catalog.bundled_provider_overlay()

    assert dict(overlay) == {}
    assert len(_overlay_warnings(caplog)) == 1
    assert catalog.load_bundled_catalog()["backends"] == BUNDLED_ROWS
