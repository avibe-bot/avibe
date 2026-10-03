"""The Avibe Agent's system prompt and the environment block of each consumed input.

The system prompt is stable so providers can cache it: Pi's coding-agent framing
and tool snippets, then Avibe's injected sections. Facts that change (cwd, date,
live Watches) travel in an ``<environment>`` block rendered into each consumed
input instead (C-7 tools.md section 8), so the stored transcript is what the
model saw.

Preamble, tool snippets, and rules ported from Pi (MIT, Copyright (c) 2025 Mario
Zechner), ``packages/coding-agent/src/core/system-prompt.ts`` and
``packages/coding-agent/src/core/tools/{read,write,edit,bash}.ts`` at ``7fbbd5f``;
Pi-specific lines (its docs, ``PI_*`` variables) are left out.
"""

from __future__ import annotations

import functools
import os
import platform
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import TextBlock, UserMessage

PREAMBLE = (
    "You are an expert coding assistant operating inside Avibe, a local-first agent runtime. You help users by "
    "reading files, executing commands, editing code, and writing new files."
)
TOOL_SNIPPETS: dict[str, str] = {
    "read": "Read file contents",
    "bash": "Execute bash commands (ls, grep, find, etc.)",
    "edit": "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "write": "Create or overwrite files",
}
TOOL_GUIDELINES: dict[str, tuple[str, ...]] = {
    "read": ("Use read to examine files instead of cat or sed.",),
    "edit": (
        "Use edit for precise changes (edits[].oldText must match exactly)",
        "When changing multiple separate locations in one file, use one edit call with multiple entries in "
        "edits[] instead of multiple edit calls",
        "Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not "
        "emit overlapping or nested edits. Merge nearby changes into one edit.",
        "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large "
        "unchanged regions.",
    ),
    "write": ("Use write only for new files or complete rewrites.",),
}
TOOL_ORDER = ("read", "bash", "edit", "write")


def coding_prompt(tool_names: Iterable[str]) -> str:
    """Pi's preamble, tool list, and rules for the tools actually offered."""
    offered = set(tool_names)
    names = [name for name in TOOL_ORDER if name in offered]
    tools = "\n".join(f"- {name}: {TOOL_SNIPPETS[name]}" for name in names) or "(none)"
    rules: list[str] = []
    if "bash" in names:
        rules.append("Use bash for file operations like ls, rg, find")
    for name in names:
        rules.extend(TOOL_GUIDELINES.get(name, ()))
    rules += ["Be concise in your responses", "Show file paths clearly when working with files"]
    return "\n\n".join(
        (
            PREAMBLE,
            f"Available tools:\n{tools}",
            "Guidelines:\n" + "\n".join(f"- {rule}" for rule in rules),
        )
    )


def system_prompt(tool_names: Iterable[str], avibe_sections: str) -> str:
    return "\n\n".join(part for part in (coding_prompt(tool_names), avibe_sections.strip()) if part)


# --- environment block ---------------------------------------------------------

ENVIRONMENT_FIELDS = ("cwd", "os", "shell", "date", "timezone", "watches")
_BLOCK = re.compile(r"\A<environment>\n(?P<body>(?:[a-z]+: [^\n]*\n)*)</environment>\Z")


@functools.lru_cache(maxsize=1)
def operating_system() -> str:
    machine = platform.machine() or "unknown"
    if platform.system() == "Darwin":
        release = platform.mac_ver()[0] or platform.release()
        return f"macOS {release} ({machine})"
    return f"{platform.system() or 'unknown'} {platform.release()} ({machine})".strip()


def local_timezone() -> str:
    zone = os.environ.get("TZ", "").lstrip(":")
    if zone:
        return zone
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.tzname[0] if time.tzname else "UTC"


def current_environment(
    cwd: str, watches: Sequence[str], *, include_time: bool = True, now: Optional[datetime] = None
) -> dict[str, str]:
    """The environment fields; the clock ones follow ``include_time_info``, as every input prefix does."""
    fields = {
        "cwd": str(Path(cwd).resolve()) if cwd else "",
        "os": operating_system(),
        "shell": os.environ.get("SHELL") or "/bin/sh",
    }
    if include_time:
        fields.update(date=(now or datetime.now()).date().isoformat(), timezone=local_timezone())
    fields["watches"] = "; ".join(watches) if watches else "none"
    # One line per field: a value never carries the block's own line structure.
    return {name: " ".join(value.split()) if "\n" in value else value for name, value in fields.items()}


def render_environment(fields: Mapping[str, str]) -> str:
    lines = [f"{name}: {fields[name]}" for name in ENVIRONMENT_FIELDS if name in fields]
    return "<environment>\n" + "".join(f"{line}\n" for line in lines) + "</environment>"


def parse_environment(text: str) -> Optional[dict[str, str]]:
    match = _BLOCK.match(text or "")
    if match is None:
        return None
    fields = dict(line.split(": ", 1) for line in match.group("body").splitlines())
    return {name: value for name, value in fields.items() if name in ENVIRONMENT_FIELDS}


def environment_state(entries: Sequence[ContextEntry]) -> dict[str, str]:
    """The environment the model was last told, replaying each input's block in context order."""
    state: dict[str, str] = {}
    for entry in entries:
        if entry.kind != "input" or not isinstance(entry.message, UserMessage):
            continue
        first = entry.message.content[0]
        if isinstance(first, TextBlock) and first.text is not None:
            delta = parse_environment(first.text)
            if delta is not None:
                state.update(delta)
    return state


def environment_delta(previous: Mapping[str, str], current: Mapping[str, str]) -> dict[str, str]:
    """Fields that changed since the model last saw them; every field on the first input."""
    return {name: value for name, value in current.items() if previous.get(name) != value}


def with_environment(message: UserMessage, delta: Mapping[str, str]) -> UserMessage:
    if not delta:
        return message
    return UserMessage(content=(TextBlock(text=render_environment(delta)), *message.content))
