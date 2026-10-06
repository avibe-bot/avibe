"""C-8/A7: a backend cannot silently fall out of an unclassified universe.

Runtime and schema assertions protect the public backend vocabulary. The source
inventory catches a new literal before it silently excludes an in-process backend;
behavioral tests alone cannot discover a newly added, unexercised selector.
Tests/fixtures are examples, not declarations. Released migrations are inventoried
explicitly, without changing their historical meaning.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path
from typing import get_args

from config.v2_config import MODEL_HUB_BACKENDS
from core.handlers.model_hub.events import EventAgent
from core.handlers.model_hub.resolver import BackendName
from modules.agents.catalog import AGENT_BACKENDS, NATIVE_CLI_BACKENDS, AgentBackend, NativeCliBackend
from modules.agents.native_sessions.types import AgentName
from scripts.generate_agent_backend_catalog import render


ROOT = Path(__file__).resolve().parents[1]
AGENTS = frozenset(AGENT_BACKENDS)
NATIVE = frozenset(NATIVE_CLI_BACKENDS)

# (path, AST kind, backend members) -> (occurrences, relation, reason).
# Counts matter: copying even an already-classified literal creates an
# unclassified declaration. Member fingerprints survive identifier-only edits.
# Mappings remain literal when their per-backend values are the implementation.
PYTHON_DECLARATIONS = {
    ("config/v2_config.py", "Dict", NATIVE): (2, "native", "Config serialization/recovery plus the avault tool; the built-in backend has no section."),
    ("config/v2_config.py", "Set", frozenset({"claude", "codex"})): (3, "native_subset", "Built-in model catalogs and legacy vendor credentials."),
    ("config/v2_config.py", "Tuple", frozenset({"claude", "codex"})): (3, "native_subset", "Released two-vendor model/credential migration."),
    ("config/v2_config.py", "Pairs", NATIVE): (1, "native", "Per-CLI auto-update config objects."),
    ("core/agent_auth_service.py", "Set", frozenset({"claude", "codex"})): (3, "native_subset", "Two vendor-specific login implementations."),
    ("core/agent_auth_service.py", "Tuple", frozenset({"claude", "codex"})): (1, "native_subset", "Login-output noise tokens, not a backend selector."),
    ("core/backend_restart.py", "Dict", frozenset({"claude", "codex"})): (1, "native_subset", "Vendor-to-native-custody mapping."),
    ("core/handlers/command_handlers.py", "Dict", NATIVE): (1, "native", "Native resume command aliases."),
    ("core/handlers/message_handler.py", "Set", frozenset({"claude", "codex"})): (3, "native_subset", "Native subagent names and prompt injection."),
    ("core/handlers/model_hub/migration.py", "Dict", frozenset({"claude", "codex"})): (2, "native_subset", "Vendor credential and native environment maps."),
    ("core/handlers/model_hub/migration.py", "Dict", NATIVE): (1, "native", "Native migration vendor descriptors."),
    ("core/handlers/model_hub/migration.py", "Set", frozenset({"claude", "codex"})): (1, "native_subset", "First-party subscription import."),
    ("core/handlers/model_hub/migration.py", "Tuple", frozenset({"claude", "codex"})): (2, "native_subset", "Subscription provider Literal types."),
    ("core/handlers/model_hub/migration_files.py", "Pairs", NATIVE): (1, "native", "Native credential document paths."),
    ("core/handlers/model_hub/migration_journal.py", "Set", frozenset({"claude", "codex"})): (2, "native_subset", "OAuth journal providers."),
    ("core/handlers/model_hub/migration_persisted.py", "Dict", NATIVE): (2, "native", "Native persisted credential snapshots."),
    ("core/handlers/model_hub/service.py", "Dict", frozenset({"claude", "codex"})): (2, "native_subset", "Native API-key environment and OAuth event reasons."),
    ("core/handlers/model_hub/service.py", "Tuple", frozenset({"claude", "codex"})): (3, "native_subset", "Backends with bundled model lists."),
    ("core/services/session_fork.py", "Set", frozenset({"codex", "opencode"})): (1, "native_subset", "Native forks that can trim a running turn."),
    ("core/services/skills.py", "Dict", NATIVE): (1, "native", "askill native CLI ids."),
    ("core/session_turns.py", "Set", frozenset({"claude", "codex", "avibe"})): (1, "agent_subset", "Runtimes whose accepted turns cannot survive a service restart (process-bound, including Avibe's in-process loop)."),
    ("core/vibe_agents.py", "Dict", NATIVE): (1, "native", "Native recommendations; Avibe uses its configured Hub catalog."),
    ("modules/im/telegram.py", "Set", frozenset({"claude", "codex"})): (1, "native_subset", "Native subagent routing controls."),
    ("scripts/incus_tenant.py", "Dict", NATIVE): (1, "native", "Bootstrap config with backend-specific fields."),
    ("scripts/prepare_regression.py", "Dict", NATIVE): (2, "native", "Regression config with backend-specific fields; known legacy CLI executable paths."),
    ("vibe/api.py", "Dict", NATIVE): (1, "native", "Public runtime config plus the avault tool; the built-in backend has no section."),
    ("vibe/api.py", "Set", frozenset({"claude", "codex"})): (1, "native_subset", "OAuth relay configuration."),
    ("vibe/api.py", "Tuple", frozenset({"claude", "codex"})): (1, "native_subset", "Masked vendor credentials."),
    ("vibe/backend_model_catalog.py", "Dict", frozenset({"claude", "codex"})): (1, "native_subset", "Bundled model catalog files."),
    ("vibe/backend_model_catalog.py", "Set", frozenset({"claude", "codex"})): (1, "native_subset", "Backends with bundled model catalogs."),
    ("vibe/cli.py", "Tuple", frozenset({"claude", "codex"})): (1, "native_subset", "Native-only model picker refresh."),
    ("vibe/desktop_backends.py", "Dict", NATIVE): (1, "native", "CLI package install specifications."),
    ("vibe/global_agents_md.py", "Dict", NATIVE): (1, "native", "Native global instruction filenames."),
    ("vibe/model_hub_runtime/adapter.py", "Dict", frozenset({"claude", "codex"})): (1, "native_subset", "Native subscription vendors."),
    ("vibe/model_hub_runtime/state.py", "Dict", frozenset({"claude", "codex"})): (1, "native_subset", "Vendor aliases, including provider names."),
    ("storage/alembic/versions/20260529_0007_backfill_scope_agent_names.py", "Set", NATIVE): (1, "native", "Immutable released migration predating Avibe."),
    ("storage/alembic/versions/20260530_0008_remove_legacy_default_agent.py", "Set", NATIVE): (1, "native", "Immutable released migration predating Avibe."),
}

TYPESCRIPT_DECLARATIONS = {
    ("ui/src/context/ApiContext.tsx", frozenset({"claude", "codex"})): (
        4, "native_subset", "Account-level native auth endpoints; OpenCode uses provider auth.",
    ),
    ("ui/src/components/settings/BackendTestPanel.tsx", frozenset({"claude", "codex"})): (
        1, "native_subset", "Account-level native auth testing.",
    ),
    ("ui/src/lib/effortOptions.ts", frozenset({"claude", "codex"})): (
        1, "native_subset", "Backends with shared-default reasoning effort metadata.",
    ),
}


def _python_declarations(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Dict):
            elements = node.keys
        elif isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            elements = node.elts
        else:
            continue
        kind = type(node).__name__
        if elements and all(isinstance(item, ast.Tuple) and len(item.elts) == 2 for item in elements):
            elements = [item.elts[0] for item in elements]
            kind = "Pairs"
        values = frozenset(
            item.value for item in elements
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
        members = values & AGENTS
        if len(members) >= 2:
            yield (path.relative_to(ROOT).as_posix(), kind, members), node.lineno


def _schema_enums(value, location=""):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "enum" and isinstance(child, list) and all(isinstance(item, str) for item in child):
                if len(AGENTS & set(child)) >= 2:
                    yield location + "/enum", set(child)
            else:
                yield from _schema_enums(child, location + "/" + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _schema_enums(child, location + "/" + str(index))


def test_backend_universes_are_catalog_owned_and_literals_are_classified():
    assert set(get_args(AgentBackend)) == set(get_args(BackendName)) == set(MODEL_HUB_BACKENDS) == AGENTS
    assert set(get_args(NativeCliBackend)) == set(get_args(AgentName)) == NATIVE
    assert set(get_args(EventAgent)) == AGENTS | {"system"}  # C-8 superset: runtime events also name the system.
    assert (ROOT / "ui/src/lib/agentBackendCatalog.generated.ts").read_text() == render()

    observed = Counter()
    locations = {}
    for directory in ("config", "core", "modules", "vibe", "storage", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            if path == ROOT / "modules/agents/catalog.py":
                continue  # The sole authoritative declaration.
            for key, line in _python_declarations(path):
                observed[key] += 1
                locations.setdefault(key, []).append(line)
    assert observed == Counter({key: rule[0] for key, rule in PYTHON_DECLARATIONS.items()}), (
        "Unclassified backend literals (import the catalog or document a capability relation): "
        f"{[(key, locations[key]) for key in observed if observed[key] != PYTHON_DECLARATIONS.get(key, (0,))[0]]}; "
        f"stale classifications: {set(PYTHON_DECLARATIONS) - set(observed)}"
    )
    for (_path, _kind, members), (_count, relation, reason) in PYTHON_DECLARATIONS.items():
        assert reason
        if relation == "agent":
            assert members == AGENTS
        elif relation == "native":
            assert members == NATIVE
        elif relation == "agent_subset":
            assert members < AGENTS
        else:
            assert relation == "native_subset" and members < NATIVE

    for path in (ROOT / "docs/plans/model-hub-contracts").glob("*.json"):
        for location, members in _schema_enums(json.loads(path.read_text())):
            if path.name == "migration-scan.schema.json":
                expected = NATIVE  # Native credential migration only.
            elif path.name == "resolution-event.schema.json" and location == "/properties/agent/enum":
                expected = AGENTS | {"system"}
            elif path.name == "agent-supply.schema.json" and location == "/allOf/8/if/properties/backend/enum":
                expected = AGENTS - {"opencode"}  # Fixed menus do not expose OpenCode's provider menu.
            else:
                expected = AGENTS
            assert members == expected, (path.name, location, members)

    # TS needs its checked-in projection to infer literal types at build time.
    # Catch newly copied literal arrays/unions outside the generated declaration.
    literal_list = re.compile(r"\[\s*((?:['\"][\w-]+['\"]\s*,?\s*){2,})\]")
    literal_union = re.compile(r"['\"][\w-]+['\"](?:\s*\|\s*['\"][\w-]+['\"])+")
    observed_ts = Counter()
    locations_ts = {}
    for path in (ROOT / "ui/src").rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or ".test." in path.name or path.name.endswith(".generated.ts"):
            continue
        source = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(), flags=re.S)
        for match in [*literal_list.finditer(source), *literal_union.finditer(source)]:
            members = frozenset(re.findall(r"['\"]([\w-]+)['\"]", match.group())) & AGENTS
            if len(members) >= 2:
                key = (str(path.relative_to(ROOT)), members)
                observed_ts[key] += 1
                locations_ts.setdefault(key, []).append(match.group())
    assert observed_ts == Counter({key: rule[0] for key, rule in TYPESCRIPT_DECLARATIONS.items()}), (
        f"Import catalog-derived Agent/native types or classify capability subsets: {locations_ts}"
    )
    for (_path, members), (_count, relation, reason) in TYPESCRIPT_DECLARATIONS.items():
        assert reason and relation == "native_subset" and members < NATIVE
