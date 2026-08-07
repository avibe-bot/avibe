import ast
import json
from pathlib import Path

from core.vibe_agents import RECOMMENDED_AGENT_MODELS, VibeAgentStore
from storage.models import agents


def test_agent_creation_materializes_recommended_models(tmp_path: Path) -> None:
    store = VibeAgentStore(tmp_path / "state.db")
    try:
        builtins = {
            agent.backend: agent
            for agent in store.ensure_builtin_default_agents(RECOMMENDED_AGENT_MODELS)
        }
        for backend, expected in RECOMMENDED_AGENT_MODELS.items():
            assert builtins[backend].model == expected
            created = store.create(name=f"new-{backend}", backend=backend)
            assert created.model == expected

        explicit = store.create(name="custom", backend="claude", model="claude-sonnet-5")
        assert explicit.model == "claude-sonnet-5"
    finally:
        store.close()


def test_missing_model_migration_is_idempotent_and_preserves_user_values(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = VibeAgentStore(db_path)
    try:
        missing = store.create(name="legacy", backend="codex", model="gpt-5.5")
        explicit = store.create(name="user-set", backend="codex", model="gpt-5.4")
        with store.engine.begin() as conn:
            conn.execute(agents.update().where(agents.c.id == missing.id).values(model=None))
    finally:
        store.close()

    migrated = VibeAgentStore(db_path)
    try:
        assert migrated.require("legacy").model == "gpt-5.6-sol"
        assert migrated.require(explicit.name).model == "gpt-5.4"
        assert migrated.prefill_missing_models() == 0
    finally:
        migrated.close()


def test_clearing_agent_model_materializes_the_recommendation(tmp_path: Path) -> None:
    store = VibeAgentStore(tmp_path / "state.db")
    try:
        store.create(name="worker", backend="opencode", model="anthropic/claude-opus-5")
        updated = store.update("worker", model=None)
        assert updated.model == "openai/gpt-5.6-sol"
    finally:
        store.close()


def test_recommendations_exist_in_bundled_model_catalog() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "vibe/data/backend_models.json").read_text(encoding="utf-8"))
    backends = catalog["backends"]
    claude_models = {item["id"] for item in backends["claude"]["models"]}
    codex_models = {item["id"] for item in backends["codex"]["models"]}

    assert RECOMMENDED_AGENT_MODELS["claude"] in claude_models
    assert RECOMMENDED_AGENT_MODELS["codex"] in codex_models
    provider, opencode_model = RECOMMENDED_AGENT_MODELS["opencode"].split("/", 1)
    assert provider == "openai"
    assert opencode_model in codex_models


def test_runtime_has_no_backend_default_model_reads() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime_roots = [root / name for name in ("config", "core", "modules", "vibe", "scripts")]
    matches = []
    for runtime_root in runtime_roots:
        for path in runtime_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                attribute_read = isinstance(node, ast.Attribute) and node.attr == "default_model"
                getattr_read = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "default_model"
                )
                if attribute_read or getattr_read:
                    matches.append(str(path.relative_to(root)))
                    break
    assert matches == []
