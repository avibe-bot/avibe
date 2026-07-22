from __future__ import annotations

from pathlib import Path

import pytest

from memory_poc.environment import ConfigurationError, ProviderSettings, child_environment, discover_provider_settings, parse_dotenv


def test_parse_dotenv_accepts_quoted_values_without_exporting_them() -> None:
    values = parse_dotenv("LLM_MODEL='model-a'\nEMBEDDING_MODEL=model-b\n")

    assert values == {"LLM_MODEL": "model-a", "EMBEDDING_MODEL": "model-b"}


def test_discovery_prefers_current_worktree(tmp_path: Path) -> None:
    local = tmp_path / ".runtime" / "memory-poc"
    local.mkdir(parents=True)
    (local / ".env.poc").write_text(
        "\n".join(
            (
                "LLM_BASE_URL=local",
                "LLM_MODEL=llm",
                "LLM_API_KEY=secret",
                "EMBEDDING_BASE_URL=local",
                "EMBEDDING_MODEL=embed",
                "EMBEDDING_API_KEY=secret",
            )
        ),
        encoding="utf-8",
    )
    (local / ".env.poc").chmod(0o600)

    settings = discover_provider_settings(tmp_path)

    assert settings.source == local / ".env.poc"
    assert settings.endpoint_locality() == "remote"


def test_discovery_reports_only_missing_key_names(tmp_path: Path) -> None:
    local = tmp_path / ".runtime" / "memory-poc"
    local.mkdir(parents=True)
    (local / ".env.poc").write_text("LLM_API_KEY=REPLACE_ME\n", encoding="utf-8")
    (local / ".env.poc").chmod(0o600)

    with pytest.raises(ConfigurationError) as raised:
        discover_provider_settings(tmp_path)

    assert "REPLACE_ME" not in str(raised.value)
    assert "LLM_API_KEY" in str(raised.value)


def test_discovery_rejects_an_overly_open_configuration_file(tmp_path: Path) -> None:
    local = tmp_path / ".runtime" / "memory-poc"
    local.mkdir(parents=True)
    dotenv = local / ".env.poc"
    dotenv.write_text("LLM_API_KEY=not-used\n", encoding="utf-8")
    dotenv.chmod(0o644)

    with pytest.raises(ConfigurationError, match="provider_configuration_mode_invalid"):
        discover_provider_settings(tmp_path)


def test_child_environment_is_allowlisted_and_drops_proxy_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "not-used")
    settings = ProviderSettings(
        llm_base_url="http://127.0.0.1:1/v1",
        llm_model="llm",
        llm_api_key="not-a-real-key",
        embedding_base_url="http://127.0.0.1:1/v1",
        embedding_model="embedding",
        embedding_api_key="not-a-real-key",
        source=tmp_path / ".env.poc",
    )

    child = child_environment(
        settings,
        everos_root=tmp_path / "everos-root",
        child_home=tmp_path / "child-home",
        metrics_path=tmp_path / "metrics.jsonl",
    )

    assert "HTTPS_PROXY" not in child
    assert "HTTP_PROXY" not in child
    assert child["PYTHONNOUSERSITE"] == "1"
    assert child["EVEROS_LLM__API_KEY"] == "not-a-real-key"
    assert all(key in child for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"))
