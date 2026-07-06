from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_utils_module():
    module_path = Path(__file__).resolve().parents[1] / "modules" / "agents" / "opencode" / "utils.py"
    spec = importlib.util.spec_from_file_location("opencode_utils_for_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_utils = _load_utils_module()
build_claude_reasoning_options = _utils.build_claude_reasoning_options
format_claude_model_label = _utils.format_claude_model_label
normalize_claude_reasoning_effort = _utils.normalize_claude_reasoning_effort


def test_claude_reasoning_options_default_to_low_medium_high() -> None:
    options = build_claude_reasoning_options(None)

    assert [item["value"] for item in options] == ["__default__", "low", "medium", "high"]


def test_claude_reasoning_options_add_xhigh_for_opus_47() -> None:
    options = build_claude_reasoning_options("claude-opus-4-7")

    assert [item["value"] for item in options] == ["__default__", "low", "medium", "high", "xhigh", "max"]


def test_claude_reasoning_options_add_xhigh_and_max_for_opus_48() -> None:
    options = build_claude_reasoning_options("claude-opus-4-8")

    assert [item["value"] for item in options] == ["__default__", "low", "medium", "high", "xhigh", "max"]


def test_claude_reasoning_options_add_xhigh_and_max_for_fable_5() -> None:
    options = build_claude_reasoning_options("claude-fable-5")

    assert [item["value"] for item in options] == ["__default__", "low", "medium", "high", "xhigh", "max"]


def test_claude_reasoning_options_add_xhigh_and_max_for_sonnet_5() -> None:
    options = build_claude_reasoning_options("claude-sonnet-5")

    assert [item["value"] for item in options] == ["__default__", "low", "medium", "high", "xhigh", "max"]


def test_claude_reasoning_options_add_xhigh_and_max_for_sonnet_aliases() -> None:
    expected = ["__default__", "low", "medium", "high", "xhigh", "max"]

    assert [item["value"] for item in build_claude_reasoning_options("sonnet")] == expected
    assert [item["value"] for item in build_claude_reasoning_options("sonnet[1m]")] == expected


def test_claude_reasoning_options_add_max_for_opus_46() -> None:
    options = build_claude_reasoning_options("claude-opus-4-6")

    assert [item["value"] for item in options] == ["__default__", "low", "medium", "high", "max"]


def test_claude_reasoning_options_add_max_for_sonnet_46() -> None:
    options = build_claude_reasoning_options("claude-sonnet-4-6")

    assert [item["value"] for item in options] == ["__default__", "low", "medium", "high", "max"]


def test_claude_reasoning_options_add_xhigh_for_opus_aliases() -> None:
    assert [item["value"] for item in build_claude_reasoning_options("opus")] == [
        "__default__",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert [item["value"] for item in build_claude_reasoning_options("opus[1m]")] == [
        "__default__",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_normalize_claude_reasoning_effort_drops_invalid_efforts() -> None:
    assert normalize_claude_reasoning_effort("claude-sonnet-4-5", "max") is None
    assert normalize_claude_reasoning_effort("claude-opus-4-6", "xhigh") is None
    assert normalize_claude_reasoning_effort("claude-opus-4-7", "xhigh") == "xhigh"
    assert normalize_claude_reasoning_effort("claude-opus-4-7", "max") == "max"
    assert normalize_claude_reasoning_effort("claude-opus-4-8", "xhigh") == "xhigh"
    assert normalize_claude_reasoning_effort("claude-opus-4-8", "max") == "max"
    assert normalize_claude_reasoning_effort("claude-opus-4-6", "max") == "max"
    assert normalize_claude_reasoning_effort("claude-sonnet-5", "xhigh") == "xhigh"
    assert normalize_claude_reasoning_effort("claude-sonnet-5", "max") == "max"
    assert normalize_claude_reasoning_effort("sonnet", "xhigh") == "xhigh"
    assert normalize_claude_reasoning_effort("sonnet", "max") == "max"
    assert normalize_claude_reasoning_effort("sonnet[1m]", "xhigh") == "xhigh"
    assert normalize_claude_reasoning_effort("sonnet[1m]", "max") == "max"
    assert normalize_claude_reasoning_effort("claude-sonnet-4-6", "max") == "max"
    assert normalize_claude_reasoning_effort("claude-fable-5", "xhigh") == "xhigh"
    assert normalize_claude_reasoning_effort("claude-fable-5", "max") == "max"
    assert normalize_claude_reasoning_effort("opus", "xhigh") == "xhigh"
    assert normalize_claude_reasoning_effort("opus", "max") == "max"


def test_claude_1m_context_labels() -> None:
    assert format_claude_model_label("claude-opus-4-8") == "claude-opus-4-8 [1M]"
    assert format_claude_model_label("claude-opus-4-7") == "claude-opus-4-7 [1M]"
    assert format_claude_model_label("claude-opus-4-6") == "claude-opus-4-6 [1M]"
    assert format_claude_model_label("claude-sonnet-5") == "claude-sonnet-5 [1M]"
    assert format_claude_model_label("claude-sonnet-4-6") == "claude-sonnet-4-6 [1M]"
    assert format_claude_model_label("claude-fable-5") == "claude-fable-5 [1M]"
    assert format_claude_model_label("opus[1m]") == "opus[1m] [1M]"
    assert format_claude_model_label("sonnet") == "sonnet [1M]"
    assert format_claude_model_label("sonnet[1m]") == "sonnet[1m] [1M]"
    assert format_claude_model_label("claude-opus-4-5") == "claude-opus-4-5"
