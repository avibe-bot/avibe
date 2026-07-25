from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import cli


def _run(handler, args_ns):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = handler(args_ns)
    raw = out.getvalue().strip() or err.getvalue().strip()
    return code, json.loads(raw)


def _models_ns(**overrides):
    base = {"name": None, "backend": None, "provider": None, "model": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_parser_registers_agent_models():
    ns = cli.build_parser().parse_args(["agent", "models", "my-agent"])
    assert ns.agent_command == "models"
    assert ns.name == "my-agent"

    ns2 = cli.build_parser().parse_args(["agent", "models", "--backend", "codex", "--provider", "x"])
    assert ns2.name is None
    assert ns2.backend == "codex"
    assert ns2.provider == "x"


def test_cmd_agent_models_by_backend(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "agent_model_options",
        lambda *a, **k: {
            "ok": True,
            "backend": "codex",
            "default_model": None,
            "models": [{"value": "gpt-5.5", "default": False, "reasoning_efforts": ["high"]}],
            "providers": None,
            "source": "codex built-in list",
            "live": False,
            "notes": None,
        },
    )
    code, payload = _run(cli.cmd_agent_models, _models_ns(backend="codex"))
    assert code == 0
    assert payload["ok"] is True
    assert payload["kind"] == "agent_models"
    assert payload["backend"] == "codex"
    assert payload["agent"] is None
    assert payload["current"] is None
    assert payload["models"][0]["value"] == "gpt-5.5"


def test_cmd_agent_models_uses_shared_pagination(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "agent_model_options",
        lambda *a, **k: {
            "ok": True,
            "backend": "codex",
            "default_model": None,
            "models": [
                {"value": f"gpt-{index:02d}", "default": False, "reasoning_efforts": ["high"]}
                for index in range(25)
            ],
            "providers": None,
            "source": "test catalog",
            "live": False,
            "notes": None,
        },
    )
    args = cli.build_parser().parse_args(["agent", "models", "--backend", "codex"])

    code, payload = _run(cli.cmd_agent_models, args)

    assert code == 0
    assert len(payload["models"]) == 20
    assert payload["pagination"]["next_command"] == (
        "vibe agent models --backend codex --page 2 --limit 20"
    )


def test_cmd_agent_models_requires_exactly_one_target():
    code, payload = _run(cli.cmd_agent_models, _models_ns())
    assert code == 1
    assert payload["ok"] is False
    assert payload["code"] == "invalid_agent_models_target"


def test_cmd_agent_models_rejects_provider_for_non_opencode():
    code, payload = _run(cli.cmd_agent_models, _models_ns(backend="claude", provider="foo"))
    assert code == 1
    assert payload["code"] == "provider_not_supported"


def test_cmd_agent_models_by_name_echoes_current(monkeypatch):
    agent = SimpleNamespace(
        name="my-audit", backend="claude", model="claude-opus-4-8", reasoning_effort="high"
    )
    monkeypatch.setattr(cli, "_agent_store", lambda: SimpleNamespace(require=lambda name: agent))
    monkeypatch.setattr(
        cli.api,
        "agent_model_options",
        lambda *a, **k: {
            "ok": True,
            "backend": "claude",
            "default_model": None,
            "models": [
                {"value": "claude-opus-4-8", "default": True, "reasoning_efforts": ["high", "max"]}
            ],
            "providers": None,
            "source": "catalog",
            "live": False,
            "notes": None,
        },
    )
    code, payload = _run(cli.cmd_agent_models, _models_ns(name="my-audit"))
    assert code == 0
    assert payload["agent"] == "my-audit"
    assert payload["backend"] == "claude"
    assert payload["current"]["model_known"] is True
    assert payload["current"]["reasoning_effort_valid"] is True
    assert payload["current"]["valid"] is True


def test_cmd_agent_models_current_flags_invalid_effort(monkeypatch):
    agent = SimpleNamespace(
        name="drifted", backend="claude", model="claude-opus-4-8", reasoning_effort="bogus"
    )
    monkeypatch.setattr(cli, "_agent_store", lambda: SimpleNamespace(require=lambda name: agent))
    monkeypatch.setattr(
        cli.api,
        "agent_model_options",
        lambda *a, **k: {
            "ok": True,
            "backend": "claude",
            "default_model": None,
            "models": [{"value": "claude-opus-4-8", "default": True, "reasoning_efforts": ["high"]}],
            "providers": None,
            "source": "catalog",
            "live": False,
            "notes": None,
        },
    )
    code, payload = _run(cli.cmd_agent_models, _models_ns(name="drifted"))
    assert code == 0
    assert payload["current"]["reasoning_effort_valid"] is False
    assert payload["current"]["valid"] is False


def test_cmd_agent_models_model_filter_keeps_current_honest(monkeypatch):
    agent = SimpleNamespace(
        name="my-audit", backend="claude", model="claude-opus-4-8", reasoning_effort="high"
    )
    monkeypatch.setattr(cli, "_agent_store", lambda: SimpleNamespace(require=lambda name: agent))
    monkeypatch.setattr(
        cli.api,
        "agent_model_options",
        lambda *a, **k: {
            "ok": True,
            "backend": "claude",
            "default_model": None,
            "models": [
                {"value": "claude-opus-4-8", "default": True, "reasoning_efforts": ["high", "max"]},
                {"value": "claude-sonnet-4-6", "default": False, "reasoning_efforts": ["high"]},
            ],
            "providers": None,
            "source": "catalog",
            "live": False,
            "notes": None,
        },
    )
    # query the Agent but narrow the display to a different model
    code, payload = _run(
        cli.cmd_agent_models, _models_ns(name="my-audit", model="claude-sonnet-4-6")
    )
    assert code == 0
    # --model narrows the displayed list ...
    assert [m["value"] for m in payload["models"]] == ["claude-sonnet-4-6"]
    # ... but current still reflects the Agent's real (unfiltered) model and stays valid
    assert payload["current"]["model"] == "claude-opus-4-8"
    assert payload["current"]["model_known"] is True
    assert payload["current"]["valid"] is True


def test_agent_models_current_normalizes_opencode_bare_model():
    # OpenCode Agent stored a bare model id; it routes via the default provider, so the
    # current check must normalize it to provider/model before declaring it unknown.
    options = {
        "default_provider": "deepseek",
        "models": [{"value": "deepseek/deepseek-chat", "reasoning_efforts": ["low", "high"]}],
    }
    agent = SimpleNamespace(model="deepseek-chat", reasoning_effort="high")
    current = cli._agent_models_current(agent, options)
    assert current["model"] == "deepseek-chat"  # echoes the original value
    assert current["model_known"] is True  # resolved via default_provider
    assert current["reasoning_effort_valid"] is True
    assert current["valid"] is True


def test_agent_value_warning_effort_only_unknown(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "agent_model_options",
        lambda *a, **k: {
            "ok": True,
            "models": [{"value": "gpt-5.5", "reasoning_efforts": ["minimal", "low", "high"]}],
        },
    )
    agent = SimpleNamespace(name="x", backend="codex", model=None, reasoning_effort="bogus")
    fields = cli._agent_value_warning_fields(agent)
    assert "warnings" in fields
    assert "bogus" in fields["warnings"][0]
    assert "vibe agent models x" in fields["hint"]


def test_agent_value_warning_effort_only_valid(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "agent_model_options",
        lambda *a, **k: {
            "ok": True,
            "models": [{"value": "gpt-5.5", "reasoning_efforts": ["minimal", "low", "high"]}],
        },
    )
    agent = SimpleNamespace(name="x", backend="codex", model=None, reasoning_effort="high")
    assert cli._agent_value_warning_fields(agent) == {}
