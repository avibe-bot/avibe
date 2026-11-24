from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

try:
    import tomllib as _toml_parser
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _toml_parser
    except ModuleNotFoundError:  # pragma: no cover
        _toml_parser = None

import yaml

logger = logging.getLogger(__name__)


_PREFIX_PATTERN = re.compile(r"^([^\s:：]+)\s*[:：]\s*(.*)$", re.DOTALL)


def _yaml_safe_load(text: str) -> dict:
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: Optional[str] = None
    developer_instructions: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    path: Optional[Path] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class PrefixMatch:
    name: str
    message: str


def parse_subagent_prefix(message: str) -> Optional[PrefixMatch]:
    if not message:
        return None

    trimmed = message.lstrip()
    match = _PREFIX_PATTERN.match(trimmed)
    if not match:
        return None

    name = match.group(1).strip()
    body = match.group(2)
    if not name:
        return None

    if not body or not body.strip():
        return None

    return PrefixMatch(name=name, message=body.strip())


def normalize_subagent_name(name: str) -> str:
    return (name or "").strip().lower()


def list_claude_subagents(
    search_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Dict[str, SubagentDefinition]:
    """List all available Claude subagents.
    
    Args:
        search_root: Global search root (default: ~/.claude)
        project_root: Project-specific root to also search (e.g., <cwd>)
    
    Returns:
        Dict mapping normalized agent names to their definitions.
        Project agents override global agents with same name.
    """
    definitions: Dict[str, SubagentDefinition] = {}
    
    # Search global agents first
    root = search_root or (Path.home() / ".claude")
    if root.exists():
        agent_files: list[Path] = []
        candidate_dirs = _find_claude_agent_dirs(root)
        for directory in candidate_dirs:
            agent_files.extend(directory.glob("*.md"))

        for agent_file in agent_files:
            definition = _parse_claude_agent_definition(agent_file)
            if not definition:
                continue
            key = normalize_subagent_name(definition.name)
            if key:
                definitions[key] = definition
    
    # Search project agents (override global)
    if project_root:
        project_agents_dir = project_root / ".claude" / "agents"
        if project_agents_dir.exists() and project_agents_dir.is_dir():
            for agent_file in project_agents_dir.glob("*.md"):
                definition = _parse_claude_agent_definition(agent_file)
                if not definition:
                    continue
                key = normalize_subagent_name(definition.name)
                if key:
                    definitions[key] = definition
    
    return definitions


def load_claude_subagent(
    name: str,
    search_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Optional[SubagentDefinition]:
    """Load a specific Claude subagent by name.
    
    Args:
        name: Agent name to search for
        search_root: Global search root (default: ~/.claude)
        project_root: Project-specific root to also search (e.g., <cwd>)
    
    Returns:
        SubagentDefinition if found, None otherwise.
        Project agents take precedence over global agents.
    """
    normalized = normalize_subagent_name(name)
    if not normalized:
        return None
    return list_claude_subagents(search_root, project_root).get(normalized)


def list_codex_subagents(
    search_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Dict[str, SubagentDefinition]:
    """List all available Codex custom agents."""
    definitions: Dict[str, SubagentDefinition] = {}

    root = search_root or (Path.home() / ".codex")
    global_agents_dir = root / "agents"
    if global_agents_dir.exists() and global_agents_dir.is_dir():
        for agent_file in sorted(global_agents_dir.glob("*.toml")):
            definition = _parse_codex_agent_definition(agent_file, source="global")
            if not definition:
                continue
            key = normalize_subagent_name(definition.name)
            if key:
                definitions[key] = definition

    if project_root:
        project_agents_dir = project_root / ".codex" / "agents"
        if project_agents_dir.exists() and project_agents_dir.is_dir():
            for agent_file in sorted(project_agents_dir.glob("*.toml")):
                definition = _parse_codex_agent_definition(agent_file, source="project")
                if not definition:
                    continue
                key = normalize_subagent_name(definition.name)
                if key:
                    definitions[key] = definition

    return definitions


def load_codex_subagent(
    name: str,
    search_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Optional[SubagentDefinition]:
    normalized = normalize_subagent_name(name)
    if not normalized:
        return None
    return list_codex_subagents(search_root, project_root).get(normalized)


def list_antigravity_subagents(
    search_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Dict[str, SubagentDefinition]:
    """List all available Antigravity native agents.

    Searches global ``~/.gemini/config/agents/*.md`` first, then project
    ``<workspace>/.agents/agents/*.md`` (project definitions override global).
    """
    definitions: Dict[str, SubagentDefinition] = {}

    root = search_root or (Path.home() / ".gemini" / "config")
    global_agents_dir = root / "agents"
    if global_agents_dir.exists() and global_agents_dir.is_dir():
        for agent_file in sorted(global_agents_dir.glob("*.md")):
            definition = _parse_antigravity_agent_definition(agent_file, source="global")
            if not definition:
                continue
            key = normalize_subagent_name(definition.name)
            if key:
                definitions[key] = definition

    if project_root:
        project_agents_dir = project_root / ".agents" / "agents"
        if project_agents_dir.exists() and project_agents_dir.is_dir():
            for agent_file in sorted(project_agents_dir.glob("*.md")):
                definition = _parse_antigravity_agent_definition(agent_file, source="project")
                if not definition:
                    continue
                key = normalize_subagent_name(definition.name)
                if key:
                    definitions[key] = definition

    return definitions


def load_antigravity_subagent(
    name: str,
    search_root: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Optional[SubagentDefinition]:
    """Load a specific Antigravity native agent by name.

    Project agents in ``<workspace>/.agents/agents/`` take precedence over
    global agents in ``~/.gemini/config/agents/``.
    """
    normalized = normalize_subagent_name(name)
    if not normalized:
        return None
    return list_antigravity_subagents(search_root, project_root).get(normalized)


def _find_claude_agent_dirs(root: Path) -> Iterable[Path]:
    agent_dirs = []
    if (root / "agents").exists():
        agent_dirs.append(root / "agents")

    for path in root.rglob("agents"):
        if path.is_dir():
            agent_dirs.append(path)

    seen = set()
    unique = []
    for path in agent_dirs:
        resolved = os.path.normpath(str(path))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _parse_markdown_frontmatter(path: Path) -> Optional[tuple[dict, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to read subagent definition file %s: %s", path, e)
        return None

    if not text.startswith("---"):
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    try:
        data = _yaml_safe_load(parts[1])
    except Exception as e:
        logger.debug("Failed to parse YAML header in subagent definition %s: %s", path, e)
        return None

    return data, parts[2]


def _clean_optional_str(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_claude_agent_definition(path: Path) -> Optional[SubagentDefinition]:
    parsed = _parse_markdown_frontmatter(path)
    if parsed is None:
        return None
    data, _body = parsed

    name = _clean_optional_str(data.get("name"))
    if not name:
        return None

    model = _clean_optional_str(data.get("model"))
    reasoning_effort = _clean_optional_str(
        data.get("reasoning_effort") or data.get("reasoningEffort")
    )

    return SubagentDefinition(name=name, model=model, reasoning_effort=reasoning_effort, path=path)


def _parse_antigravity_agent_definition(path: Path, source: str) -> Optional[SubagentDefinition]:
    parsed = _parse_markdown_frontmatter(path)
    if parsed is None:
        return None
    data, body = parsed

    name = _clean_optional_str(data.get("name"))
    if not name:
        return None

    description = _clean_optional_str(data.get("description"))
    developer_instructions = _clean_optional_str(body)
    model = _clean_optional_str(data.get("model"))
    reasoning_effort = _clean_optional_str(
        data.get("thought_level")
        or data.get("thoughtLevel")
        or data.get("reasoning_effort")
        or data.get("reasoningEffort")
    )

    return SubagentDefinition(
        name=name,
        description=description,
        developer_instructions=developer_instructions,
        model=model,
        reasoning_effort=reasoning_effort,
        path=path,
        source=source,
    )


def _parse_codex_agent_definition(path: Path, source: str) -> Optional[SubagentDefinition]:
    if _toml_parser is None:
        logger.debug("tomllib unavailable; cannot parse Codex subagent file %s", path)
        return None

    try:
        data = _toml_parser.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Failed to parse Codex subagent definition %s: %s", path, e)
        return None

    if not isinstance(data, dict):
        return None

    name = data.get("name")
    description = data.get("description")
    developer_instructions = data.get("developer_instructions")
    model = data.get("model")
    reasoning_effort = data.get("model_reasoning_effort")

    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(developer_instructions, str) or not developer_instructions.strip():
        return None

    if not isinstance(model, str) or not model.strip():
        model = None
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        reasoning_effort = None

    return SubagentDefinition(
        name=name.strip(),
        description=description.strip(),
        developer_instructions=developer_instructions.strip(),
        model=model,
        reasoning_effort=reasoning_effort,
        path=path,
        source=source,
    )
