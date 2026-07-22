from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_poc.environment import (
    ConfigurationError,
    ProviderSettings,
    assert_clean_harness_source,
    child_environment,
    discover_provider_settings,
    parse_dotenv,
    verify_harness_interpreter,
)
from memory_poc.errors import HarnessError


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


def test_discovery_rejects_a_symlinked_local_runtime_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    local = outside / "memory-poc"
    local.mkdir(parents=True)
    dotenv = local / ".env.poc"
    dotenv.write_text("LLM_API_KEY=not-used\n", encoding="utf-8")
    dotenv.chmod(0o600)
    (tmp_path / ".runtime").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="provider_configuration_path_unsafe"):
        discover_provider_settings(tmp_path)


def test_discovery_rejects_a_symlinked_fallback_runtime_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = tmp_path / "fallback"
    outside = tmp_path / "outside"
    dotenv_dir = outside / "memory-poc"
    dotenv_dir.mkdir(parents=True)
    dotenv = dotenv_dir / ".env.poc"
    dotenv.write_text("LLM_API_KEY=not-used\n", encoding="utf-8")
    dotenv.chmod(0o600)
    fallback.mkdir()
    (fallback / ".runtime").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("memory_poc.environment._FALLBACK_WORKTREE", fallback)

    with pytest.raises(ConfigurationError, match="provider_configuration_path_unsafe"):
        discover_provider_settings(tmp_path)


def test_discovery_rejects_a_model_value_that_matches_any_api_key(tmp_path: Path) -> None:
    local = tmp_path / ".runtime" / "memory-poc"
    local.mkdir(parents=True)
    dotenv = local / ".env.poc"
    dotenv.write_text(
        "\n".join(
            (
                "LLM_BASE_URL=local",
                "LLM_MODEL=embedding-secret",
                "LLM_API_KEY=llm-secret",
                "EMBEDDING_BASE_URL=local",
                "EMBEDDING_MODEL=embedding",
                "EMBEDDING_API_KEY=embedding-secret",
            )
        ),
        encoding="utf-8",
    )
    dotenv.chmod(0o600)

    with pytest.raises(ConfigurationError, match="provider_model_matches_secret"):
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
        python=tmp_path / "alternate-runtime" / "bin" / "python",
        everos_root=tmp_path / "everos-root",
        child_home=tmp_path / "child-home",
        metrics_path=tmp_path / "metrics.jsonl",
        owner_id="00000000-0000-4000-8000-000000000001",
        anchor=tmp_path,
    )

    assert "HTTPS_PROXY" not in child
    assert "HTTP_PROXY" not in child
    assert child["PYTHONNOUSERSITE"] == "1"
    assert child["PATH"].startswith(str(tmp_path / "alternate-runtime" / "bin"))
    assert child["EVEROS_LLM__API_KEY"] == "not-a-real-key"
    assert child["MEMORY_POC_OWNER_ID"] == "00000000-0000-4000-8000-000000000001"
    assert all(key in child for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"))


def test_harness_interpreter_rejects_a_host_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = tmp_path / "env" / "bin" / "python"
    monkeypatch.setattr("memory_poc.environment.locked_environment_python", lambda _root: expected)
    monkeypatch.setattr("memory_poc.environment.sys.executable", str(tmp_path / "host" / "bin" / "python"))

    with pytest.raises(HarnessError, match="harness_interpreter_not_locked"):
        verify_harness_interpreter(tmp_path)


def test_harness_interpreter_rejects_a_shared_base_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_python = tmp_path / "base" / "bin" / "python"
    base_python.parent.mkdir(parents=True)
    base_python.touch()
    expected = tmp_path / "env" / "bin" / "python"
    expected.parent.mkdir(parents=True)
    expected.symlink_to(base_python)
    monkeypatch.setattr("memory_poc.environment.locked_environment_python", lambda _root: expected)
    monkeypatch.setattr("memory_poc.environment.sys.executable", str(base_python))
    monkeypatch.setattr("memory_poc.environment.sys.prefix", str(tmp_path / "base"))

    with pytest.raises(HarnessError, match="harness_interpreter_not_locked"):
        verify_harness_interpreter(tmp_path)


def test_clean_harness_source_rejects_uncommitted_harness_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "memory_poc.environment.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=" M scripts/memory_poc/harness/src/memory_poc/sanity.py\n"),
    )

    with pytest.raises(HarnessError, match="harness_source_dirty"):
        assert_clean_harness_source(tmp_path)
