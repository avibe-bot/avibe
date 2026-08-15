from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "scripts/generate_model_hub_mock_corpus.py"
CORPUS_PATH = (
    ROOT / "ui/src/components/settings/models/modelHubMockCorpus.json"
)


def _generator_module():
    spec = importlib.util.spec_from_file_location(
        "generate_model_hub_mock_corpus",
        GENERATOR_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_mh_mock_replay_001_corpus_is_server_generated_and_keyed_by_full_state():
    """MH-MOCK-REPLAY-001: every record carries full, hash-checked state."""

    result = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert len(corpus["sequences"]) == 5
    assert len(corpus["transitions"]) == 11
    generator = _generator_module()
    assert {
        item["operation"] for item in corpus["recording_operations"]
    } == set(generator.OPERATION_DISPATCH)
    for transition in corpus["transitions"]:
        assert transition["key"]["pre"] == {
            "model_hub_config_sha256": _digest(transition["pre"]["config"]),
            "fixture_world_sha256": _digest(
                transition["pre"]["fixture_world"]
            ),
        }
        assert transition["post"]["model_hub_config_sha256"] == _digest(
            transition["post"]["config"]
        )
        assert transition["post"]["fixture_world_sha256"] == _digest(
            transition["post"]["fixture_world"]
        )
        assert set(transition["post"]["reads"]) == {
            "sources",
            "agents",
            "agent_sources",
            "agent_chains",
        }


def test_mh_mock_replay_001_fixture_world_rejects_unregistered_access():
    """MH-MOCK-REPLAY-001: the recording world is sealed, not best-effort."""

    generator = _generator_module()
    seed = json.loads(generator.SEED_PATH.read_text(encoding="utf-8"))
    runtime = generator.FixtureRuntime(
        seed["config"],
        seed["fixture_world"],
    )
    with pytest.raises(generator.UnregisteredFixtureAccess):
        runtime.adapter.status()
    with generator.sealed_execution(runtime):
        with pytest.raises(generator.UnregisteredFixtureAccess):
            open(ROOT / "pyproject.toml", encoding="utf-8")
        with pytest.raises(generator.UnregisteredFixtureAccess):
            generator.service_module.datetime.now()
        with pytest.raises(generator.UnregisteredFixtureAccess):
            generator.service_module.uuid.uuid4()


def test_mh_mock_replay_001_record_miss_extends_a_known_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The advertised command can extend a non-seed state and reuse its prefix."""

    generator = _generator_module()
    seed = json.loads(generator.SEED_PATH.read_text(encoding="utf-8"))
    sequence_spec = json.loads(
        generator.SEQUENCES_PATH.read_text(encoding="utf-8")
    )
    corpus, traces = asyncio.run(generator._generate(seed, sequence_spec))
    previous = traces["reorder-then-delete-regression"][-1]
    key_body = {
        "version": 1,
        "pre": {
            "model_hub_config_sha256": previous["post"]["model_hub_config_sha256"],
            "fixture_world_sha256": previous["post"]["fixture_world_sha256"],
        },
        "request": {
            "operation": "setAgentMode",
            "path": {"backend": "claude"},
            "query": {},
            "body": {"present": True, "value": {"mode": "direct"}},
        },
    }
    token = generator._token(key_body)
    sequences_path = tmp_path / "model_hub_mock_sequences.json"
    sequences_path.write_text(generator._render(sequence_spec), encoding="utf-8")
    monkeypatch.setattr(generator, "SEQUENCES_PATH", sequences_path)

    generator._record_miss(token, sequence_spec, corpus, traces)
    extended = json.loads(sequences_path.read_text(encoding="utf-8"))
    regenerated, _ = asyncio.run(generator._generate(seed, extended))

    assert len(regenerated["sequences"]) == 6
    assert len(regenerated["transitions"]) == 12
    assert regenerated["transitions"][-1]["key"]["request"] == key_body["request"]


def test_mh_mock_replay_001_advertised_operations_have_server_dispatches():
    """Every advertised record command enters an authoritative service method."""

    generator = _generator_module()
    seed = json.loads(generator.SEED_PATH.read_text(encoding="utf-8"))

    async def dispatch_all() -> None:
        for spec in generator.OPERATION_DISPATCH.values():
            runtime = generator.FixtureRuntime(
                seed["config"],
                seed["fixture_world"],
            )
            try:
                with generator.sealed_execution(runtime):
                    await generator._execute(
                        runtime,
                        copy.deepcopy(spec.recording_probe),
                    )
            except generator.ModelHubError:
                pass

    asyncio.run(dispatch_all())
