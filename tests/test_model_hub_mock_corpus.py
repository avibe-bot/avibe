from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
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
    assert corpus["artifact"] == "model-hub-mock-corpus-v1"
    assert len(corpus["sequences"]) == 5
    assert len(corpus["transitions"]) == 11
    generator = _generator_module()
    assert {
        item["operation"] for item in corpus["operation_registry"]
    } == set(generator.OPERATION_REGISTRY)
    for item in corpus["operation_registry"]:
        assert item["recording"]["request"]["operation"] == item["operation"]
        assert item["request_identity"]["strategy"] == "all_except_declared"
        assert set(item["request_identity"]) == {
            "strategy",
            "sensitive_fields",
            "sensitive_placeholder",
            "volatile_fields",
            "volatile_placeholder",
            "eligibility",
        }
        assert item["request_identity"]["sensitive_placeholder"] == (
            generator.SENSITIVE_PLACEHOLDER
        )
        assert item["request_identity"]["volatile_placeholder"] == (
            generator.VOLATILE_PLACEHOLDER
        )
        if item["dispatch"] == "unrecordable":
            assert item["recording"]["command"] is None
            assert item["reachability"]["kind"] == "unrecordable"
            assert "#1462" in item["reachability"]["reason"]
            continue
        assert item["dispatch"] == "authoritative_server"
        assert item["recording"]["command"] == generator.GENERATOR_COMMAND
        if item["reachability"]["kind"] == "seed":
            assert item["reachability"]["prerequisites"] == []
        else:
            assert item["reachability"]["kind"] == "sequence"
            assert item["reachability"]["prerequisites"]
    for transition in corpus["transitions"]:
        assert re.fullmatch(r"[0-9a-f]{64}", transition["key"]["id"])
        with pytest.raises((ValueError, UnicodeDecodeError, json.JSONDecodeError)):
            generator._decode_token(transition["key"]["id"])
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
        "version": 2,
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
    transition_id = generator._sha256(key_body)
    request_token = generator._token(key_body)
    sequences_path = tmp_path / "model_hub_mock_sequences.json"
    sequences_path.write_text(generator._render(sequence_spec), encoding="utf-8")
    monkeypatch.setattr(generator, "SEQUENCES_PATH", sequences_path)

    generator._record_miss(
        transition_id,
        request_token,
        sequence_spec,
        corpus,
        traces,
    )
    extended = json.loads(sequences_path.read_text(encoding="utf-8"))
    regenerated, _ = asyncio.run(generator._generate(seed, extended))

    assert len(regenerated["sequences"]) == 6
    assert len(regenerated["transitions"]) == 12
    assert regenerated["transitions"][-1]["key"]["request"] == key_body["request"]


def test_mh_mock_replay_001_operation_registry_is_total_and_reachable():
    """Every registry entry declares and proves all generator-owned facets."""

    generator = _generator_module()
    seed = json.loads(generator.SEED_PATH.read_text(encoding="utf-8"))
    assert generator.OPERATION_REGISTRY
    for operation, spec in generator.OPERATION_REGISTRY.items():
        assert spec.recording_probe["operation"] == operation
        assert spec.request_identity.eligibility.kind in {
            "always",
            "observation_fixture",
            "unrecordable",
        }
        assert not (
            set(spec.request_identity.sensitive_fields)
            & set(spec.request_identity.volatile_fields)
        )
        if spec.reachability.kind == "unrecordable":
            assert spec.handler is None
            assert spec.reachability.reason
        else:
            assert callable(spec.handler)
            assert spec.reachability.kind in {"seed", "sequence"}
            assert (spec.reachability.kind == "seed") != bool(
                spec.reachability.prerequisites
            )

    asyncio.run(generator._validate_operation_registry(seed))


def test_mh_mock_replay_001_request_identity_redacts_sensitive_and_volatile_fields():
    """Registry declarations structurally exclude unsafe request values from ids."""

    generator = _generator_module()

    def replace(request: dict, path: tuple[str, ...], value: str) -> None:
        parent = request
        for member in path[:-1]:
            parent = parent[member]
        parent[path[-1]] = value

    for operation, spec in generator.OPERATION_REGISTRY.items():
        for index, path in enumerate(spec.request_identity.sensitive_fields):
            sensitive = f"sensitive-{operation}-{index}"
            request = json.loads(json.dumps(spec.recording_probe))
            replace(request, path, sensitive)
            canonical = generator._canonical_request(request)
            key_input = {
                "version": 2,
                "pre": {
                    "model_hub_config_sha256": "a" * 64,
                    "fixture_world_sha256": "b" * 64,
                },
                "request": canonical,
            }
            transition_id = generator._sha256(key_input)
            command = (
                f"{generator.GENERATOR_COMMAND} --record-miss {transition_id} "
                f"--request-token {generator._token(key_input)}"
            )
            assert sensitive not in generator._canonical(canonical)
            assert sensitive not in transition_id
            assert sensitive not in command

        for index, path in enumerate(spec.request_identity.volatile_fields):
            first = json.loads(json.dumps(spec.recording_probe))
            second = json.loads(json.dumps(spec.recording_probe))
            replace(first, path, f"volatile-a-{index}")
            replace(second, path, f"volatile-b-{index}")
            assert generator._canonical_request(first) == generator._canonical_request(
                second
            )
