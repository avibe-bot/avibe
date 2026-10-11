"""Vibey's system prompt and the environment block of each consumed input.

The system prompt is stable so providers can cache it: who the Agent is, Pi's
tool snippets and rules, then Avibe's injected sections. Who the Agent is comes
from its record, so it is the same on every Turn the Session runs as that Agent.
Facts that change (cwd, date, live Watches) travel in an ``<environment>`` block
rendered into each consumed input instead (C-7 tools.md section 8), so the
stored transcript is what the model saw.

Tool snippets and rules ported from Pi (MIT, Copyright (c) 2025 Mario Zechner),
``packages/coding-agent/src/core/system-prompt.ts`` and
``packages/coding-agent/src/core/tools/{read,write,edit,bash}.ts`` at ``7fbbd5f``;
Pi-specific lines (its docs, ``PI_*`` variables) are left out. Avibe's deviations:
the preamble (owner product decision, 2026-10-10) replaces Pi's coding-assistant
framing. The backend's built-in Agent is Vibey, Avibe's own agent
(``VIBEY_PREAMBLE``); any other Agent on the backend gets ``AGENT_PREAMBLE``, and its
own definition, in the Avibe sections, says who it is. ``FINAL_REPLY_RULE`` is
Avibe's too: text between tool calls reaches the user only above the shared interim
threshold (``ConsolidatedMessageDispatcher._is_interim_worthy``).
"""

from __future__ import annotations

import functools
import os
import platform
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

from core.agent_core.harness.context import display, escape
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import TextBlock, UserMessage

VIBEY_PREAMBLE = (
    "You are Vibey, the official agent of Avibe, a local-first Agent OS that runs on the user's own machine. You help "
    "the user with whatever they are working on: you read files, run commands, edit and write files, and coordinate "
    "work through Avibe."
)
AGENT_PREAMBLE = "You are an agent running inside Avibe, a local-first Agent OS on the user's machine."
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
FINAL_REPLY_RULE = "Only your final reply is reliably shown to the user; put anything the user must see in it"
#: Avibe's: the loop runs a response's neighbouring read and bash calls at the same time (C-3 loop-control.md section 2).
CONCURRENT_CALLS_RULE = (
    "Send independent calls (reads, searches, separate commands) together in one response: they run at the same "
    "time. Edits and writes run one at a time, in order. A call that needs an earlier call's result goes in a later "
    "response"
)
#: The first input of a fork (C-10 fork.md section 8): the inherited history is the source's, and so is its work.
FORK_NOTICE = (
    "<fork>\nThis Session is a fork of {title} (Session {source}): {history}. Every command, Watch, Task, and run "
    "in that Session stays with the Session that started it and reports there, not here.\n</fork>"
)
FORK_HISTORY = "the conversation above is that Session's history through context_seq {seq}"
FORK_NO_HISTORY = "it starts empty, before that Session's first finished Turn, so no history is inherited"


def preamble(builtin_agent: bool) -> str:
    """Who the Agent is: Vibey for the backend's built-in Agent, else a neutral line its own definition completes."""
    return VIBEY_PREAMBLE if builtin_agent else AGENT_PREAMBLE


def coding_prompt(tool_names: Iterable[str], *, builtin_agent: bool, rg: bool) -> str:
    """The Agent's preamble, then Pi's tool list and rules for the tools actually offered.

    ``rg`` is whether the Session's commands find ripgrep; the bash rule names it only then, and ``grep`` otherwise.
    """
    offered = set(tool_names)
    names = [name for name in TOOL_ORDER if name in offered]
    tools = "\n".join(f"- {name}: {TOOL_SNIPPETS[name]}" for name in names) or "(none)"
    rules: list[str] = []
    if "bash" in names:
        rules.append(f"Use bash for file operations like ls, {'rg' if rg else 'grep'}, find")
    for name in names:
        rules.extend(TOOL_GUIDELINES.get(name, ()))
    if names:
        rules.append(CONCURRENT_CALLS_RULE)
    rules += ["Be concise in your responses", "Show file paths clearly when working with files", FINAL_REPLY_RULE]
    return "\n\n".join(
        (
            preamble(builtin_agent),
            f"Available tools:\n{tools}",
            "Guidelines:\n" + "\n".join(f"- {rule}" for rule in rules),
        )
    )


def system_prompt(tool_names: Iterable[str], avibe_sections: str, *, builtin_agent: bool, rg: bool) -> str:
    head = coding_prompt(tool_names, builtin_agent=builtin_agent, rg=rg)
    return "\n\n".join(part for part in (head, avibe_sections.strip()) if part)


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


#: The environment block names at most this many Watches (tools.md section 8).
_WATCHES_LISTED = 20

#: A field's raw value: text, or the Watches' lines.
EnvironmentValue = Union[str, tuple[str, ...]]


def current_environment(
    cwd: str, watches: Sequence[str], *, include_time: bool = True, now: Optional[datetime] = None
) -> dict[str, EnvironmentValue]:
    """The environment fields, raw; the clock ones follow ``include_time_info``, as every input prefix does.

    Identity is the raw value: only the block the model reads displays them (``render_environment``), and only the
    exact display of one can prove it unchanged (``environment_delta``).
    """
    fields: dict[str, EnvironmentValue] = {
        "cwd": str(Path(cwd).resolve()) if cwd else "",
        "os": operating_system(),
        "shell": os.environ.get("SHELL") or "/bin/sh",
    }
    if include_time:
        fields.update(date=(now or datetime.now()).date().isoformat(), timezone=local_timezone())
    fields["watches"] = tuple(watches)
    return fields


def render_environment(fields: Mapping[str, EnvironmentValue]) -> str:
    shown = displayed_environment(fields)
    lines = [f"{name}: {shown[name]}" for name in ENVIRONMENT_FIELDS if name in shown]
    return "<environment>\n" + "".join(f"{line}\n" for line in lines) + "</environment>"


def displayed_environment(fields: Mapping[str, EnvironmentValue]) -> dict[str, str]:
    """Each field as the model reads it, one line of plain text: the cwd escaped and never cut (the model builds
    absolute paths from it, so a cut one would be false; the OS bounds its length), every other value, each Watch's
    line included, cut to 160 bytes (``display``); the first Watches, then how many more."""
    return {name: _shown(name, value) for name, value in fields.items()}


def _shown(name: str, value: EnvironmentValue) -> str:
    if name != "watches":
        return escape(str(value)) if name == "cwd" else display(str(value))
    # Bounded on every input, and so in every checkpoint: the first ones, then how many more (tools.md section 8).
    listed = [display(line) for line in value[:_WATCHES_LISTED]]
    if len(value) > _WATCHES_LISTED:
        listed.append(f"and {len(value) - _WATCHES_LISTED} more")
    return "; ".join(listed) if listed else "none"


def _whole(name: str, value: EnvironmentValue) -> bool:
    """Whether the model reads all of the value: escaping is injective, so only a cut can hide a change."""
    if name != "watches":
        return _shown(name, value) == escape(str(value))
    return all(display(line) == escape(line) for line in value[:_WATCHES_LISTED])


def parse_environment(text: str) -> Optional[dict[str, str]]:
    match = _BLOCK.match(text or "")
    if match is None:
        return None
    fields = dict(line.split(": ", 1) for line in match.group("body").splitlines())
    return {name: value for name, value in fields.items() if name in ENVIRONMENT_FIELDS}


def environment_state(entries: Sequence[ContextEntry]) -> dict[str, str]:
    """The environment the model was last told, as it read it, replaying each input's block in context order."""
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


def environment_delta(
    previous: Mapping[str, str], current: Mapping[str, EnvironmentValue]
) -> dict[str, EnvironmentValue]:
    """The fields the model must be told: every field on the first input, then each one whose display differs from
    what the model last read (``previous``), or is cut.

    Identity is the raw value, but the rows hold only what the model read: escaping is injective, so a whole display
    proves its value unchanged, while a cut one can look the same after its value changed and is sent on every input.
    """
    shown = displayed_environment(current)
    return {
        name: value for name, value in current.items() if previous.get(name) != shown[name] or not _whole(name, value)
    }


def with_fork_notice(message: UserMessage, *, source_session_id: str, source_title: str, through_seq: int) -> UserMessage:
    """A fork's first input names whose history the model inherited and who owns the work in it (C-10 section 8).

    Placed before the input's own content; the environment block, when there is one, still comes first.
    """
    notice = FORK_NOTICE.format(
        title=display(source_title) if source_title else "an earlier Session",
        source=display(source_session_id),
        history=FORK_HISTORY.format(seq=int(through_seq)) if through_seq else FORK_NO_HISTORY,
    )
    return UserMessage(content=(TextBlock(text=notice), *message.content))


def with_environment(message: UserMessage, delta: Mapping[str, EnvironmentValue]) -> UserMessage:
    if not delta:
        return message
    return UserMessage(content=(TextBlock(text=render_environment(delta)), *message.content))
