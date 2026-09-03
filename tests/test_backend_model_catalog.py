import builtins
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe import backend_model_catalog


class _FakeResponse:
    def __init__(self, payload: dict, *, headers: dict[str, str] | None = None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture(autouse=True)
def _reset_remote_cache(monkeypatch):
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", False)
    yield
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()


def test_reasoning_effort_authorities_are_ordered_and_complete() -> None:
    assert backend_model_catalog.REASONING_EFFORT_VOCABULARY == (
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    )
    assert backend_model_catalog.PROTOCOL_REASONING_EFFORT_DEFAULTS == {
        "openai_responses": ("minimal", "low", "medium", "high", "xhigh"),
        "openai_chat": ("minimal", "low", "medium", "high", "xhigh"),
        "anthropic": ("low", "medium", "high", "xhigh", "max"),
    }
    assert all(
        "ultra" not in efforts
        for efforts in backend_model_catalog.PROTOCOL_REASONING_EFFORT_DEFAULTS.values()
    )


def test_bundled_catalog_rows_are_exact_and_covered_by_the_vocabulary() -> None:
    catalog = backend_model_catalog.load_bundled_catalog()
    efforts_by_model = (
        backend_model_catalog.bundled_catalog_reasoning_efforts_by_model()
    )
    rows = [
        model
        for backend in catalog["backends"].values()
        for model in backend["models"]
        if "reasoning_efforts" in model
    ]
    declared = {
        effort
        for model in rows
        for effort in model["reasoning_efforts"]
    }

    assert rows
    assert declared <= set(backend_model_catalog.REASONING_EFFORT_VOCABULARY)
    assert len(backend_model_catalog.REASONING_EFFORT_VOCABULARY) == len(
        set(backend_model_catalog.REASONING_EFFORT_VOCABULARY)
    )
    for model in rows:
        assert efforts_by_model[model["id"]] == tuple(model["reasoning_efforts"])


def test_bundled_reasoning_effort_index_is_immutable_and_keeps_first_row(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        backend_model_catalog,
        "load_bundled_catalog",
        lambda: {
            "backends": {
                "first": {
                    "models": [
                        {"id": "shared-model", "reasoning_efforts": ["low"]}
                    ]
                },
                "second": {
                    "models": [
                        {"id": "shared-model", "reasoning_efforts": ["ultra"]}
                    ]
                },
            }
        },
    )

    efforts_by_model = (
        backend_model_catalog.bundled_catalog_reasoning_efforts_by_model()
    )

    assert efforts_by_model["shared-model"] == ("low",)
    with pytest.raises(TypeError):
        efforts_by_model["shared-model"] = ("changed",)


def test_backend_model_entries_normalize_runtime_catalog_shape():
    catalog = {
        "schema_version": 1,
        "backends": {
            "codex": {
                "models": [
                    "gpt-custom",
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "visibility": "list",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "ultra"},
                        ],
                    },
                ]
            }
        },
    }

    assert backend_model_catalog.backend_model_entries("codex", catalog) == [
        {"id": "gpt-custom"},
        {
            "id": "gpt-5.6-sol",
            "label": "GPT-5.6-Sol",
            "visibility": "list",
            "reasoning_efforts": ["low", "ultra"],
        },
    ]


def test_codex_hub_catalog_projects_complete_catalog(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(backend_model_catalog.paths, "get_runtime_dir", lambda: runtime_dir)

    path = backend_model_catalog._publish_codex_hub_catalog(
        json.dumps(
            {
                "client_version": "0.149.1",
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "display_name": "GPT-5.6 Luna",
                        "use_responses_lite": True,
                        "multi_agent_version": "v1",
                        "tool_mode": "code_mode_only",
                        "prefer_websockets": True,
                        "model_messages": {"instructions_template": "preserved"},
                    }
                ],
            }
        ).encode()
    )

    assert path.name.startswith("standard-responses-")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["client_version"] == "0.149.1"
    assert payload["models"][0] == {
        "slug": "gpt-5.6-luna",
        "display_name": "GPT-5.6 Luna",
        "use_responses_lite": False,
        "multi_agent_version": None,
        "tool_mode": None,
        "prefer_websockets": False,
        "model_messages": {"instructions_template": "preserved"},
    }


def test_codex_hub_catalog_projects_custom_backend_models_from_native_shape():
    raw = json.dumps(
        {
            "client_version": "0.149.1",
            "models": [
                {
                    "slug": "gpt-native",
                    "display_name": "GPT Native",
                    "priority": 99,
                    "visibility": "list",
                    "supported_in_api": True,
                    "context_window": 200_000,
                    "max_context_window": 200_000,
                    "input_modalities": ["text", "image"],
                    "supports_image_detail_original": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [{"effort": "medium", "description": "Medium"}],
                    "use_responses_lite": True,
                    "multi_agent_version": "v1",
                    "tool_mode": "code_mode_only",
                    "prefer_websockets": True,
                    "model_messages": {"instructions_template": "preserved"},
                }
            ],
        }
    ).encode()
    configured = [
        {
            "id": "deepseek-v4",
            "display_name": "DeepSeek V4",
            "context_window": 1_048_576,
            "input_modalities": ["text"],
            "reasoning_efforts": ["low", "high"],
        }
    ]

    payload = json.loads(backend_model_catalog._codex_hub_catalog_bytes(raw, configured))

    assert payload["client_version"] == "0.149.1"
    assert payload["models"] == [
        {
            "slug": "deepseek-v4",
            "display_name": "DeepSeek V4",
            "priority": 1,
            "visibility": "list",
            "supported_in_api": True,
            "context_window": 1_048_576,
            "max_context_window": 1_048_576,
            "input_modalities": ["text"],
            "supports_image_detail_original": False,
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Low"},
                {"effort": "high", "description": "High"},
            ],
            "use_responses_lite": False,
            "multi_agent_version": None,
            "tool_mode": None,
            "prefer_websockets": False,
            "model_messages": {"instructions_template": "preserved"},
            "support_verbosity": False,
            "experimental_supported_tools": [],
        }
    ]


def test_codex_hub_catalog_does_not_inherit_native_model_metadata_for_custom_rows():
    raw = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-native",
                    "display_name": "GPT Native",
                    "description": "Native-only description",
                    "priority": 1,
                    "visibility": "list",
                    "supported_in_api": True,
                    "context_window": 200_000,
                    "max_context_window": 200_000,
                    "auto_compact_token_limit": 180_000,
                    "comp_hash": "native-only",
                    "input_modalities": ["text", "image"],
                    "supports_image_detail_original": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [{"effort": "medium", "description": "Medium"}],
                    "additional_speed_tiers": ["fast"],
                    "service_tiers": [{"id": "priority", "name": "Fast", "description": "Native"}],
                    "supports_search_tool": True,
                    "shell_type": "unified_exec",
                    "support_verbosity": True,
                    "experimental_supported_tools": ["native_tool"],
                    "truncation_policy": {"mode": "tokens", "limit": 10_000},
                    "model_messages": {"instructions_template": "preserved"},
                }
            ]
        }
    ).encode()

    payload = json.loads(
        backend_model_catalog._codex_hub_catalog_bytes(
            raw,
            [{"id": "custom-model", "input_modalities": [], "reasoning_efforts": []}],
        )
    )
    custom = payload["models"][0]

    assert custom["slug"] == "custom-model"
    assert custom["model_messages"] == {"instructions_template": "preserved"}
    assert custom["shell_type"] == "unified_exec"
    assert custom["truncation_policy"] == {"mode": "tokens", "limit": 10_000}
    assert custom["input_modalities"] == ["text"]
    assert custom["supported_reasoning_levels"] == [{"effort": "none", "description": "None"}]
    assert custom["support_verbosity"] is False
    assert custom["experimental_supported_tools"] == []
    for key in (
        "description",
        "context_window",
        "max_context_window",
        "auto_compact_token_limit",
        "comp_hash",
        "additional_speed_tiers",
        "service_tiers",
        "supports_search_tool",
    ):
        assert key not in custom


def test_codex_hub_catalog_preserves_native_modalities_without_an_override():
    raw = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-native",
                    "display_name": "GPT Native",
                    "context_window": 200_000,
                    "max_context_window": 200_000,
                    "auto_compact_token_limit": 180_000,
                    "input_modalities": ["text", "image"],
                    "supports_image_detail_original": True,
                }
            ]
        }
    ).encode()

    payload = json.loads(
        backend_model_catalog._codex_hub_catalog_bytes(
            raw,
            [
                {
                    "id": "gpt-native",
                    "display_name": "GPT Native",
                    "input_modalities": [],
                }
            ],
        )
    )

    assert payload["models"][0]["input_modalities"] == ["text", "image"]
    assert payload["models"][0]["supports_image_detail_original"] is True
    assert payload["models"][0]["auto_compact_token_limit"] == 180_000


def test_codex_hub_catalog_retires_native_compaction_limit_after_context_override():
    raw = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-native",
                    "display_name": "GPT Native",
                    "context_window": 200_000,
                    "max_context_window": 200_000,
                    "auto_compact_token_limit": 180_000,
                    "effective_context_window_percent": 90,
                }
            ]
        }
    ).encode()

    payload = json.loads(
        backend_model_catalog._codex_hub_catalog_bytes(
            raw,
            [{"id": "gpt-native", "context_window": 400_000}],
        )
    )
    projected = payload["models"][0]

    assert projected["context_window"] == 400_000
    assert projected["max_context_window"] == 400_000
    assert projected["effective_context_window_percent"] == 90
    assert "auto_compact_token_limit" not in projected


def test_codex_hub_catalog_does_not_give_custom_models_template_modalities():
    raw = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-native",
                    "input_modalities": ["text", "image"],
                    "supports_image_detail_original": True,
                }
            ]
        }
    ).encode()

    payload = json.loads(
        backend_model_catalog._codex_hub_catalog_bytes(
            raw,
            [{"id": "custom-model", "input_modalities": []}],
        )
    )

    assert payload["models"][0]["input_modalities"] == ["text"]
    assert payload["models"][0]["supports_image_detail_original"] is False


def test_codex_hub_catalog_can_disable_and_restore_native_reasoning():
    raw = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-native",
                    "default_reasoning_level": "high",
                    "supported_reasoning_levels": [
                        {"effort": "low", "description": "Low"},
                        {"effort": "high", "description": "High"},
                    ],
                }
            ]
        }
    ).encode()
    configured = {
        "id": "gpt-native",
        "reasoning_efforts": ["low", "high"],
    }

    disabled = json.loads(
        backend_model_catalog._codex_hub_catalog_bytes(
            raw,
            [{**configured, "supports_reasoning": False}],
        )
    )["models"][0]
    restored = json.loads(
        backend_model_catalog._codex_hub_catalog_bytes(
            raw,
            [{**configured, "supports_reasoning": None}],
        )
    )["models"][0]

    assert disabled["default_reasoning_level"] == "none"
    assert disabled["supported_reasoning_levels"] == [{"effort": "none", "description": "None"}]
    assert restored["default_reasoning_level"] == "low"
    assert restored["supported_reasoning_levels"] == [
        {"effort": "low", "description": "Low"},
        {"effort": "high", "description": "High"},
    ]


def test_codex_hub_catalog_export_uses_stable_supervisor(monkeypatch):
    captured = {}

    async def run(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            exit_code=0,
            stdout='{"models":[{"slug":"gpt-test"}]}',
            stderr="",
            timed_out=False,
            stdout_truncated=False,
        )

    monkeypatch.setattr(backend_model_catalog, "run_supervised_command", run)

    exported = backend_model_catalog._export_codex_bundled_catalog(
        "/opt/codex",
        {"PATH": "/bin", "OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret"},
    )

    assert exported == b'{"models":[{"slug":"gpt-test"}]}'
    assert captured["command"] == [
        "/opt/codex",
        "debug",
        "models",
        "--bundled",
        "-c",
        "model_catalog_json=null",
    ]
    assert captured["extra_env"] == {
        "PATH": "/bin",
        "OPENAI_API_KEY": "secret",
        "CODEX_API_KEY": "secret",
    }
    assert captured["remove_env"] == (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "CODEX_API_KEY",
        "AVIBE_MODEL_HUB_TOKEN",
    )
    assert captured["timeout_seconds"] == backend_model_catalog.CODEX_HUB_CATALOG_TIMEOUT_SECONDS
    assert captured["max_output_bytes"] == backend_model_catalog.CODEX_HUB_CATALOG_MAX_BYTES
    assert captured["discard_stderr"] is True


def test_codex_hub_catalog_export_survives_deleted_service_cwd(monkeypatch, tmp_path):
    captured = {}

    async def run(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            exit_code=0,
            stdout='{"models":[{"slug":"gpt-test"}]}',
            stderr="",
            timed_out=False,
            stdout_truncated=False,
        )

    monkeypatch.setattr(backend_model_catalog, "run_supervised_command", run)
    deleted_cwd = tmp_path / "deleted-service-cwd"
    deleted_cwd.mkdir()
    original_cwd = Path.cwd()
    os.chdir(deleted_cwd)
    deleted_cwd.rmdir()
    try:
        exported = backend_model_catalog._export_codex_bundled_catalog("/opt/codex")
    finally:
        os.chdir(original_cwd)

    assert exported == b'{"models":[{"slug":"gpt-test"}]}'
    assert Path(captured["cwd"]).is_dir()
    assert captured["cwd"] != str(deleted_cwd)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (SimpleNamespace(timed_out=True, stdout_truncated=False, exit_code=124), "timed out"),
        (
            SimpleNamespace(timed_out=False, stdout_truncated=True, exit_code=0),
            "exceeded the safety limit",
        ),
        (SimpleNamespace(timed_out=False, stdout_truncated=False, exit_code=1), "could not export"),
    ],
)
def test_codex_hub_catalog_export_rejects_supervised_failures(monkeypatch, result, message):
    result.stdout = ""

    async def run(**_kwargs):
        return result

    monkeypatch.setattr(backend_model_catalog, "run_supervised_command", run)

    with pytest.raises(RuntimeError, match=message):
        backend_model_catalog._export_codex_bundled_catalog("/opt/codex")


def test_codex_hub_catalog_preparation_exports_current_binary(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(backend_model_catalog.paths, "get_runtime_dir", lambda: runtime_dir)
    calls = []
    monkeypatch.setattr(
        backend_model_catalog,
        "_export_codex_bundled_catalog",
        lambda binary, base_env=None: (
            calls.append((binary, base_env)),
            b'{"models":[{"slug":"gpt-test","use_responses_lite":true}]}',
        )[1],
    )

    exported = backend_model_catalog.prepare_codex_hub_catalog("codex")

    assert exported.name.startswith("standard-responses-")
    assert calls == [("codex", None)]
    assert json.loads(exported.read_text(encoding="utf-8"))["models"][0]["use_responses_lite"] is False


def test_codex_hub_catalog_failure_cannot_select_a_previous_generation(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(backend_model_catalog.paths, "get_runtime_dir", lambda: runtime_dir)
    previous = backend_model_catalog._publish_codex_hub_catalog(b'{"client_version":"old","models":[{"slug":"old"}]}')

    def fail_export(*_args, **_kwargs):
        raise RuntimeError("export failed")

    monkeypatch.setattr(backend_model_catalog, "_export_codex_bundled_catalog", fail_export)

    with pytest.raises(RuntimeError, match="export failed"):
        backend_model_catalog.prepare_codex_hub_catalog("/opt/codex")

    assert previous.exists()
    assert list(previous.parent.glob("standard-responses-*.json")) == [previous]


def test_codex_hub_catalog_generations_are_content_addressed(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(backend_model_catalog.paths, "get_runtime_dir", lambda: runtime_dir)

    first = backend_model_catalog._publish_codex_hub_catalog(b'{"models":[{"slug":"gpt-first"}]}')
    second = backend_model_catalog._publish_codex_hub_catalog(b'{"models":[{"slug":"gpt-second"}]}')

    assert first != second
    assert json.loads(first.read_text(encoding="utf-8"))["models"][0]["slug"] == "gpt-first"
    assert json.loads(second.read_text(encoding="utf-8"))["models"][0]["slug"] == "gpt-second"


def test_merge_sources_applies_tombstones_and_fills_missing_metadata():
    merged = backend_model_catalog.merge_model_sources(
        [
            (
                "remote",
                [
                    {"id": "hidden-model", "visibility": "hidden"},
                    {"id": "shared-model", "label": "Remote label"},
                ],
            ),
            (
                "local",
                [
                    {"id": "hidden-model", "reasoning_efforts": ["ultra"]},
                    {"id": "shared-model", "reasoning_efforts": ["low", "high"]},
                    {"id": "local-model"},
                ],
            ),
        ]
    )

    assert [entry["id"] for entry in merged] == ["shared-model", "local-model"]
    assert merged[0] == {
        "id": "shared-model",
        "label": "Remote label",
        "reasoning_efforts": ["low", "high"],
        "source": "remote",
    }


def test_codex_local_hidden_tombstone_overrides_remote_visible(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "account-hidden", "visibility": "hide"},
                    {"slug": "account-visible", "visibility": "list"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        backend_model_catalog,
        "load_cached_remote_catalog",
        lambda **kwargs: {
            "schema_version": 1,
            "backends": {
                "codex": {
                    "models": [
                        {"id": "account-hidden"},
                        {"id": "remote-model"},
                    ]
                }
            },
        },
    )
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    assert "account-hidden" not in snapshot["models"]
    assert snapshot["models"][:2] == ["remote-model", "account-visible"]


def test_codex_local_efforts_override_remote_metadata(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "shared-model",
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        backend_model_catalog,
        "load_cached_remote_catalog",
        lambda **kwargs: {
            "schema_version": 1,
            "backends": {
                "codex": {
                    "models": [
                        {
                            "id": "shared-model",
                            "reasoning_efforts": ["low", "high", "ultra"],
                        }
                    ]
                }
            },
        },
    )
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    assert [entry["value"] for entry in snapshot["reasoning_options"]["shared-model"]] == [
        "__default__",
        "low",
        "high",
    ]


def test_claude_snapshot_merges_configured_custom_models(monkeypatch, tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        json.dumps(
            {
                "model": "custom-claude-model",
                "env": {"ANTHROPIC_SMALL_FAST_MODEL": "custom-fast-model"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backend_model_catalog.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(backend_model_catalog, "load_cached_remote_catalog", lambda **kwargs: {})

    snapshot = backend_model_catalog.backend_model_snapshot("claude", schedule_refresh=False)

    assert "custom-claude-model" in snapshot["models"]
    assert "custom-fast-model" in snapshot["models"]
    assert snapshot["models"][:2] == ["claude-fable-5-1", "claude-fable-5"]
    assert snapshot["model_labels"]["claude-fable-5-1"] == "claude-fable-5-1 [1M]"
    assert [option["value"] for option in snapshot["reasoning_options"]["claude-fable-5-1"]] == [
        "__default__",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert snapshot["model_labels"]["claude-opus-5"] == "claude-opus-5 [1M]"
    assert snapshot["model_labels"]["claude-opus-4-6"] == "claude-opus-4-6 [1M]"


def test_remote_hidden_tombstone_overrides_stale_local_visible(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "retired-model", "visibility": "list"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        backend_model_catalog,
        "load_cached_remote_catalog",
        lambda **kwargs: {
            "schema_version": 1,
            "backends": {"codex": {"models": [{"id": "retired-model", "visibility": "hidden"}]}},
        },
    )
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    assert "retired-model" not in snapshot["models"]


def test_snapshot_returns_immediately_while_remote_refresh_runs(monkeypatch, tmp_path):
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    def slow_refresh(url=None):
        refresh_started.set()
        release_refresh.wait(timeout=2)
        return {}

    monkeypatch.setattr(backend_model_catalog, "refresh_remote_catalog_now", slow_refresh)

    started_at = time.monotonic()
    snapshot = backend_model_catalog.backend_model_snapshot("claude")
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert refresh_started.wait(timeout=1)
    assert snapshot["catalog_refresh_pending"] is True
    release_refresh.set()
    deadline = time.monotonic() + 1
    while backend_model_catalog._REMOTE_REFRESH_IN_FLIGHT and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend_model_catalog._REMOTE_REFRESH_IN_FLIGHT is False


@pytest.mark.parametrize("timestamp_key", ["fetched_at", "checked_at"])
def test_remote_catalog_revalidates_after_five_minutes(monkeypatch, timestamp_key):
    payload = {
        timestamp_key: 100.0,
        "catalog": {"schema_version": 1, "backends": {}},
        "source_key": backend_model_catalog._remote_catalog_source_key(
            backend_model_catalog.DEFAULT_REMOTE_CATALOG_URL
        ),
    }

    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 399.0)
    assert backend_model_catalog._remote_cache_stale(payload) is False

    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 400.0)
    assert backend_model_catalog._remote_cache_stale(payload) is True


def test_refresh_remote_catalog_persists_validated_cache(monkeypatch, tmp_path):
    payload = {
        "schema_version": 1,
        "backends": {"claude": {"models": [{"id": "claude-fable-6", "reasoning_efforts": ["low", "max"]}]}},
    }
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            payload,
            headers={"ETag": '"catalog-v2"', "Last-Modified": "Fri, 24 Jul 2026 18:28:54 GMT"},
        ),
    )
    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 200.0)

    catalog = backend_model_catalog.refresh_remote_catalog_now("https://example.test/catalog.json")

    assert backend_model_catalog.backend_model_entries("claude", catalog)[0]["id"] == "claude-fable-6"
    persisted = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert persisted["catalog"] == catalog
    assert persisted["fetched_at"] == 200.0
    assert persisted["checked_at"] == 200.0
    assert persisted["etag"] == '"catalog-v2"'
    assert persisted["last_modified"] == "Fri, 24 Jul 2026 18:28:54 GMT"
    assert persisted["source_key"] == backend_model_catalog._remote_catalog_source_key(
        "https://example.test/catalog.json"
    )
    assert persisted["error"] is None


def test_refresh_remote_catalog_uses_etag_and_preserves_cache_on_304(monkeypatch, tmp_path):
    previous_catalog = {
        "schema_version": 1,
        "backends": {"claude": {"models": [{"id": "claude-opus-5"}]}},
    }
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    backend_model_catalog._write_cached_remote_payload(
        {
            "fetched_at": 100.0,
            "checked_at": 150.0,
            "catalog": previous_catalog,
            "etag": '"catalog-v1"',
            "error": None,
            "source_key": backend_model_catalog._remote_catalog_source_key("https://example.test/catalog.json"),
        }
    )

    def not_modified(request, timeout):
        assert request.get_header("If-none-match") == '"catalog-v1"'
        assert request.get_header("Cache-control") == "no-cache"
        raise backend_model_catalog.urllib.error.HTTPError(
            request.full_url,
            304,
            "Not Modified",
            {"ETag": '"catalog-v1"'},
            None,
        )

    monkeypatch.setattr(backend_model_catalog.urllib.request, "urlopen", not_modified)
    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 200.0)

    catalog = backend_model_catalog.refresh_remote_catalog_now("https://example.test/catalog.json")

    assert catalog == previous_catalog
    persisted = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert persisted["catalog"] == previous_catalog
    assert persisted["fetched_at"] == 100.0
    assert persisted["checked_at"] == 200.0
    assert persisted["etag"] == '"catalog-v1"'
    assert persisted["source_key"] == backend_model_catalog._remote_catalog_source_key(
        "https://example.test/catalog.json"
    )
    assert persisted["error"] is None


def test_refresh_remote_catalog_does_not_reuse_validator_for_another_url(monkeypatch, tmp_path):
    previous_catalog = {
        "schema_version": 1,
        "backends": {"claude": {"models": [{"id": "claude-opus-4-8"}]}},
    }
    next_catalog = {
        "schema_version": 1,
        "backends": {"claude": {"models": [{"id": "claude-opus-5"}]}},
    }
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    backend_model_catalog._write_cached_remote_payload(
        {
            "fetched_at": 100.0,
            "checked_at": 150.0,
            "catalog": previous_catalog,
            "etag": '"old-source"',
            "source_key": backend_model_catalog._remote_catalog_source_key("https://old.example.test/catalog.json"),
            "error": None,
        }
    )

    def changed_source(request, timeout):
        assert request.get_header("If-none-match") is None
        return _FakeResponse(next_catalog)

    monkeypatch.setattr(backend_model_catalog.urllib.request, "urlopen", changed_source)
    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 200.0)

    catalog = backend_model_catalog.refresh_remote_catalog_now("https://new.example.test/catalog.json")

    assert catalog == next_catalog
    persisted = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert "etag" not in persisted
    assert persisted["source_key"] == backend_model_catalog._remote_catalog_source_key(
        "https://new.example.test/catalog.json"
    )


def test_malformed_refresh_preserves_last_good_catalog(monkeypatch, tmp_path):
    previous_catalog = {
        "schema_version": 1,
        "backends": {"claude": {"models": [{"id": "claude-fable-6"}]}},
    }
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse({"schema_version": 2, "models": []}),
    )
    backend_model_catalog._write_cached_remote_payload(
        {
            "fetched_at": 100.0,
            "checked_at": 150.0,
            "catalog": previous_catalog,
            "etag": '"catalog-v1"',
            "error": None,
            "source_key": backend_model_catalog._remote_catalog_source_key("https://example.test/catalog.json"),
        }
    )
    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 200.0)
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", True)

    backend_model_catalog._refresh_remote_catalog_worker()

    persisted = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert persisted["catalog"] == previous_catalog
    assert persisted["fetched_at"] == 100.0
    assert persisted["checked_at"] == 150.0
    assert persisted["etag"] == '"catalog-v1"'
    assert persisted["source_key"] == backend_model_catalog._remote_catalog_source_key(
        "https://example.test/catalog.json"
    )
    assert persisted["failed_at"] == 200.0
    assert "Unsupported backend model catalog schema version" in persisted["error"]


def test_fetch_remote_catalog_rejects_invalid_model_entries(monkeypatch):
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            {
                "schema_version": 1,
                "backends": {"codex": {"models": [{"label": "missing id"}]}},
            }
        ),
    )

    with pytest.raises(ValueError, match="invalid model entry"):
        backend_model_catalog.fetch_remote_catalog("https://example.test/catalog.json")


@pytest.mark.parametrize(
    "model_entry, error",
    [
        ({"id": "gpt-invalid", "visibility": "sometimes"}, "invalid visibility"),
        ({"id": "gpt-invalid", "priority": "first"}, "invalid priority"),
        ({"id": "gpt-invalid", "reasoning_efforts": "ultra"}, "invalid reasoning efforts"),
    ],
)
def test_fetch_remote_catalog_rejects_malformed_model_metadata(monkeypatch, model_entry, error):
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            {
                "schema_version": 1,
                "backends": {"codex": {"models": [model_entry]}},
            }
        ),
    )

    with pytest.raises(ValueError, match=error):
        backend_model_catalog.fetch_remote_catalog("https://example.test/catalog.json")


def test_fetch_remote_catalog_rejects_unknown_backend(monkeypatch):
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            {
                "schema_version": 1,
                "backends": {"codxe": {"models": [{"id": "gpt-typo"}]}},
            }
        ),
    )

    with pytest.raises(ValueError, match="unsupported backend"):
        backend_model_catalog.fetch_remote_catalog("https://example.test/catalog.json")


def test_bundled_codex_56_efforts_include_ultra():
    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    values = {entry["value"] for entry in snapshot["reasoning_options"]["gpt-5.6-terra"]}
    assert "ultra" in values


def test_codex_catalog_readers_expand_codex_home(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex-state"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "gpt-expanded", "visibility": "list"}]}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text('model = "gpt-configured"', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", "~/codex-state")

    assert backend_model_catalog._read_codex_models_cache() == [{"id": "gpt-expanded", "visibility": "list"}]
    assert backend_model_catalog._read_codex_config_models()[0]["id"] == "gpt-configured"


def test_backend_builtin_models_merge_backend_snapshots_but_exclude_user_config(
    monkeypatch,
):
    monkeypatch.setattr(
        backend_model_catalog,
        "_cached_remote_payload",
        lambda: {"catalog": {}},
    )
    monkeypatch.setattr(backend_model_catalog, "_read_complete_catalog", lambda _path: {})
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_codex_models_cache_with_status",
        lambda: ([], True),
    )
    monkeypatch.setattr(
        backend_model_catalog,
        "_codex_sources",
        lambda *_args: [
            ("remote", [{"id": "gpt-remote", "label": "GPT Remote"}]),
            ("bundled", [{"id": "gpt-bundled", "reasoning_efforts": ["high"]}]),
            ("local", [{"id": "gpt-local"}]),
            ("legacy", [{"id": "gpt-legacy"}]),
            ("config", [{"id": "gpt-user-configured"}]),
        ],
    )

    snapshot = backend_model_catalog.backend_builtin_models(
        "codex",
        schedule_refresh=False,
    )

    assert [item["id"] for item in snapshot] == [
        "gpt-remote",
        "gpt-bundled",
        "gpt-local",
        "gpt-legacy",
    ]
    assert snapshot[0]["display_name"] == "GPT Remote"
    assert snapshot[1]["reasoning_efforts"] == ["high"]
    assert snapshot[2]["reasoning_efforts"] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_builtin_snapshot_requires_each_catalog_once_when_cli_is_installed(
    monkeypatch,
):
    reads = {"remote": 0, "bundled": 0, "local": 0}

    def remote(_path):
        reads["remote"] += 1
        return {
            "catalog": {},
            "source_key": backend_model_catalog._remote_catalog_source_key(
                backend_model_catalog.DEFAULT_REMOTE_CATALOG_URL
            ),
        }

    def bundled(_path):
        reads["bundled"] += 1
        return {}

    def local():
        reads["local"] += 1
        return [], False

    monkeypatch.setattr(backend_model_catalog, "_read_cached_remote_payload", remote)
    monkeypatch.setattr(backend_model_catalog, "_read_complete_catalog", bundled)
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_codex_models_cache_with_status",
        local,
    )

    snapshot = backend_model_catalog.backend_builtin_snapshot(
        "codex",
        cli_installed=True,
        schedule_refresh=False,
    )

    assert snapshot["complete"] is False
    assert reads == {"remote": 1, "bundled": 1, "local": 1}


def test_builtin_snapshot_does_not_require_cli_cache_when_cli_is_absent(monkeypatch):
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_cached_remote_payload",
        lambda _path: {
            "catalog": {},
            "source_key": backend_model_catalog._remote_catalog_source_key(
                backend_model_catalog.DEFAULT_REMOTE_CATALOG_URL
            ),
        },
    )
    monkeypatch.setattr(backend_model_catalog, "_read_complete_catalog", lambda _path: {})
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_codex_models_cache_with_status",
        lambda: ([], False),
    )

    snapshot = backend_model_catalog.backend_builtin_snapshot(
        "codex",
        cli_installed=False,
        schedule_refresh=False,
    )

    assert snapshot["complete"] is True


def test_builtin_snapshot_rejects_remote_cache_from_previous_url(monkeypatch):
    old_url = "https://old.example.test/catalog.json"
    new_url = "https://new.example.test/catalog.json"
    refreshes = []
    monkeypatch.setenv(backend_model_catalog.REMOTE_CATALOG_URL_ENV, new_url)
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_cached_remote_payload",
        lambda _path: {
            "catalog": {
                "schema_version": 1,
                "backends": {
                    "codex": {"models": [{"id": "gpt-from-old-source"}]}
                },
            },
            "source_key": backend_model_catalog._remote_catalog_source_key(old_url),
        },
    )
    monkeypatch.setattr(
        backend_model_catalog,
        "schedule_remote_catalog_refresh",
        lambda: refreshes.append(True) or True,
    )
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_complete_catalog",
        lambda _path: {},
    )
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_codex_models_cache_with_status",
        lambda: ([], False),
    )

    snapshot = backend_model_catalog.backend_builtin_snapshot(
        "codex",
        cli_installed=False,
    )

    assert snapshot["complete"] is False
    assert "gpt-from-old-source" not in {
        item["id"] for item in snapshot["models"]
    }
    assert refreshes == [True]


def test_builtin_snapshot_rereads_remote_cache_file_and_changes_generation(
    monkeypatch,
    tmp_path,
):
    remote_path = tmp_path / "backend_model_catalog.json"
    bundled = {"schema_version": 1, "backends": {"claude": {"models": []}, "codex": {"models": []}}}
    monkeypatch.setattr(backend_model_catalog, "get_cached_catalog_path", lambda: remote_path)
    monkeypatch.setattr(backend_model_catalog, "_read_complete_catalog", lambda _path: bundled)
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_codex_models_cache_with_status",
        lambda: ([], True),
    )

    def write_remote(model_id):
        remote_path.write_text(
            json.dumps(
                {
                    "source_key": backend_model_catalog._remote_catalog_source_key(
                        backend_model_catalog.DEFAULT_REMOTE_CATALOG_URL
                    ),
                    "catalog": {
                        "schema_version": 1,
                        "backends": {
                            "claude": {"models": []},
                            "codex": {"models": [{"id": model_id}]},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

    write_remote("gpt-file-generation-one")
    first = backend_model_catalog.backend_builtin_snapshot("codex", schedule_refresh=False)
    backend_model_catalog._REMOTE_MEMORY_CACHE.update(
        {"catalog": {"schema_version": 1, "backends": {"codex": {"models": [{"id": "gpt-stale"}]}}}}
    )
    write_remote("gpt-file-generation-two")
    second = backend_model_catalog.backend_builtin_snapshot("codex", schedule_refresh=False)

    assert first["generation"] != second["generation"]
    assert first["models"][0]["id"] == "gpt-file-generation-one"
    assert second["models"][0]["id"] == "gpt-file-generation-two"


def test_parse_toml_falls_back_to_tomli_when_tomllib_is_unavailable(monkeypatch):
    real_import = builtins.__import__
    fallback = SimpleNamespace(loads=lambda raw: {"model": "gpt-python-310"})

    def fake_import(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'")
        if name == "tomli":
            return fallback
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert backend_model_catalog._parse_toml('model = "gpt-python-310"') == {"model": "gpt-python-310"}
