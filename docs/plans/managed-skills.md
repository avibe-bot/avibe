# Avibe Managed Skills v1

> **Status:** Accepted
> **Date:** 2026-09-02
> **Scope:** Runtime discovery, catalog injection, loading, backend isolation,
> and built-in Skill lifecycle for Claude Code, Codex, and OpenCode

## 1. Decision

Avibe owns one Skill experience across all supported agent backends:

1. Users install a Skill once.
2. Avibe discovers compatible global, project, backend-native, and built-in
   locations.
3. Every backend receives the same resolved Skill Catalog.
4. An agent loads a Skill through `vibe skill load <name>`.

The agent sees Skill names and descriptions when choosing a Skill. It sees the
selected Skill's absolute directory only when loading it, because supporting
scripts, references, and assets are resolved relative to that directory.

Avibe's Catalog and load command are the product-level entry points. Backend
namespaces, source locations, conflict resolution, and compatibility metadata
are implementation details and are not exposed to the agent.

This is product-path isolation, not a filesystem security boundary. An agent
can still read a known file through ordinary filesystem tools.

## 2. Why Skills First

Backend-native prompt files are not uniform:

- their global and project paths differ;
- their traversal and injection behavior differ; and
- not every backend provides a reliable way to disable automatic context-file
  loading.

Avibe therefore does not attempt to unify `AGENTS.md`, `CLAUDE.md`, or global
system prompts in v1. That work requires a dependable native opt-out first.

Skills have a smaller and already portable unit: a directory containing a
`SKILL.md`, optionally accompanied by scripts and references. Avibe keeps that
storage shape and replaces only native discovery and loading in the Avibe
runtime path.

## 3. Compatibility Boundary

The on-disk unit remains compatible with the Agent Skills convention:

```text
example-skill/
|-- SKILL.md
|-- scripts/
|-- references/
`-- assets/
```

Avibe requires only two non-empty fields in the leading frontmatter:

```yaml
---
name: example-skill
description: Explain when this Skill should be used.
---
```

The Catalog prompt and `vibe skill load` output are Avibe runtime contracts.
They are not presented as formats required by the Agent Skills specification.

Existing Skills are usable by default. There is no Avibe-specific portability
field, compatibility state, validation state, source namespace, or migration
requirement.

## 4. Discovery

### 4.1 Candidate shape

A discovery root contributes only direct child Skill directories:

```text
<discovery-root>/<skill-directory>/SKILL.md
```

Avibe does not recursively treat arbitrary nested `SKILL.md` files as separate
Skills. Directories such as `scripts/`, `references/`, and `assets/` remain
part of their parent Skill.

Backend-bundled or system Skills, plugin caches, administrator-only Skills,
and `.claude/commands` are not discovery sources.

### 4.2 Built-in root

```text
${AVIBE_HOME:-$HOME/.avibe}/builtin-skills
```

This is Avibe-owned runtime content and has the highest resolution priority.

### 4.3 Project roots

For each directory `D` from the active working directory up to and including
the Git project root, Avibe inspects:

```text
<D>/.agents/skills
<D>/.codex/skills
<D>/.claude/skills
<D>/.opencode/skills
```

The nearest directory to the active working directory wins within the same
directory family. When there is no Git root, only the active working directory
is considered project scope.

### 4.4 Global roots

```text
$HOME/.agents/skills
${CODEX_HOME:-$HOME/.codex}/skills
$HOME/.claude/skills
${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills
```

Native locations are compatibility inputs. Avibe reads them in place and does
not migrate, rename, or annotate their Skills.

### 4.5 Reserved root

```text
${AVIBE_HOME:-$HOME/.avibe}/skills
```

This path is reserved for a future user-level Avibe Skill role. In v1 Avibe
does not scan it, write to it, or assign it any behavior.

## 5. Parsing and Resolution

### 5.1 Loose frontmatter parsing

Discovery is deliberately permissive:

- extract `name` and `description` from the leading frontmatter;
- accept the Skill when both values are present and non-empty;
- ignore every other field;
- do not reject a Skill because unrelated frontmatter is unknown or malformed;
- do not require the declared name to match the directory name; and
- fold a multiline description into one line for Catalog display.

If either required value cannot be extracted, omit that candidate without
failing the whole Catalog.

### 5.2 Deduplication

Candidates are deduplicated by their final declared `name`. The winner is
selected by these rules, in order:

1. Avibe built-ins over project and global Skills.
2. Project Skills over global Skills.
3. Within project scope, a directory nearer to the working directory over a
   more distant directory.
4. At the same scope and depth, directory-family priority is:
   `.avibe` > `.agents` > `.codex` > `.claude` > OpenCode.

The `.avibe` family reserves the highest family slot for future use, but
`${AVIBE_HOME:-$HOME/.avibe}/skills` is inactive in v1. OpenCode means
`.opencode` at project scope and `${XDG_CONFIG_HOME:-$HOME/.config}/opencode`
at global scope.

Resolution must be deterministic. After resolution, entries are sorted by
name for pagination and prompt rendering.

### 5.3 Agent-visible result

The agent receives only each winning Skill's `name` and `description` in the
Catalog. It does not receive:

- the discovery root or backend source;
- a namespace or scope label;
- duplicate candidates;
- the conflict rule or selected priority; or
- compatibility or validation status.

## 6. Skill Catalog Contract

### 6.1 System prompt injection

When at least one Skill is available, Avibe injects this block with the
resolved rows substituted:

```md
## Skills

Skills provide specialized instructions and workflows for specific tasks.
When a task matches a skill's description, run `vibe skill load <name>` before proceeding.
If the user requests a skill by name, load it.
Only load skill names listed here or returned by `vibe skill list`; do not guess names.

### Available skills
- data-analysis: Analyze datasets, generate charts, and create reports.
- pdf-processing: Extract text, fill forms, and merge PDF files.
```

The system prompt contains page 1 only, with at most 25 Skills. If more entries
exist, append exactly:

```md
More skills are available. Run `vibe skill list --page 2` to view more.
```

When no Skills are available, omit the entire block.

### 6.2 List command

```text
vibe skill list
vibe skill list --page <N>
```

The default page is page 1. Each page contains at most 25 entries in the same
stable name order used by the prompt. Standard output uses only Catalog rows:

```text
- data-analysis: Analyze datasets, generate charts, and create reports.
- pdf-processing: Extract text, fill forms, and merge PDF files.
```

When another page exists, the output ends with the same next-page sentence
used by the prompt, with the appropriate page number. Paths, sources, scopes,
and resolution metadata are not printed.

## 7. Skill Load Contract

### 7.1 Command

```text
vibe skill load <name>
```

The command resolves the live Catalog, selects the current winner for `name`,
and writes this structure to standard output:

```xml
<skill_content name="pdf-processing" directory="/absolute/path/to/pdf-processing">
SKILL BODY ONLY
</skill_content>
```

Contract details:

- `name` is the resolved Skill name.
- `directory` is the absolute, agent-accessible directory containing
  `SKILL.md`.
- XML attribute values are escaped.
- Only the body after the leading frontmatter is emitted.
- The body is otherwise unchanged: no generated title, indentation, summary,
  or rewrite is added.
- Relative references to `scripts/`, `references/`, `assets/`, or other files
  resolve from `directory`.
- There is no protocol/version header, JSON envelope, source label,
  compatibility state, or file manifest.

On failure, standard output is empty, standard error contains a short
human-readable error, and the process exits non-zero.

The wrapper frames tool output; it does not change instruction precedence. A
loaded Skill is tool-level context and cannot override Avibe's system prompt,
permissions, or safety rules.

## 8. Freshness and Session Semantics

### 8.1 New Turn snapshot

Immediately before each actual new backend Turn, Avibe:

1. resolves the Catalog from the current filesystem state;
2. renders the current system prompt with page 1 of that Catalog; and
3. dispatches the Turn to the existing backend Session.

This gives an existing Avibe Session newly installed, renamed, edited, or
removed Skills on its next Turn. No Avibe restart and no new Session are
required.

A message steered into a backend Turn that is already active is part of that
same Turn and does not trigger another Catalog refresh. Changes become visible
on the next actual Turn.

### 8.2 Live commands

`vibe skill list` and `vibe skill load` resolve from disk on every invocation.
Therefore:

- adding or removing a Skill changes the next command result and next Turn;
- changing `name` or `description` changes the next Catalog resolution; and
- changing the body, scripts, or references changes the next load or file
  read.

### 8.3 Historical context is historical

Avibe does not track which Skills a Session has loaded, attach revisions to
loaded content, rewrite old conversation history, invalidate prior loads, or
require a Skill to be reloaded on every Turn.

Once a Skill body has been loaded, it remains historical context just like any
other file previously read by the agent. The current Catalog describes current
availability; it does not retroactively change history.

### 8.4 No watcher in v1

There is no filesystem watcher, change notification, incremental system-prompt
patch, or long-lived per-Session Catalog. Turn-time rendering is already the
correct consistency boundary, so a second refresh mechanism would add state
without improving the user-visible contract.

The initial implementation performs a full scan of the fixed roots and reads
only enough of each `SKILL.md` to extract frontmatter. A reference measurement
of six roots, 20 Skill files, and 242 KB total input completed in about 1 ms
median on a warm local filesystem. This is not a latency guarantee; it shows
that correctness can precede caching.

If production measurements later justify a cache, directory enumeration still
runs every Turn and parsed frontmatter may be reused by file identity, size,
and nanosecond modification time. Caching must preserve the same freshness
contract.

## 9. Backend Isolation and Prompt Application

Avibe disables native Skill presentation in the call path it controls, then
applies the same Avibe Catalog to all three backends.

| Backend | Native Skill isolation | Applying a changed Avibe prompt |
| --- | --- | --- |
| Claude Code | Configure the SDK with `skills=[]`. | Build the candidate prompt every Turn. If it changed, recreate the SDK client and resume the same native session; otherwise reuse the client. |
| Codex | Set `skills.include_instructions=false`. | Build `developerInstructions` every Turn. Send updated instructions only when they differ, while retaining the same thread/session. |
| OpenCode | Set the effective Agent permission `skill=deny` and send `tools.skill=false` in every prompt request. | Build and send the current system prompt on every new Turn. |

For OpenCode, `tools.skill=false` is a request parameter, not text appended to
the system prompt.

The v1 isolation boundary intentionally does not block:

- handwritten Codex `$skill` references;
- backend TUI or slash-command endpoints that Avibe does not invoke; or
- ordinary filesystem access to known Skill paths.

Avibe does not add command deny-lists, directory fingerprints, transport
rebuilds solely for isolation, or backend-specific compatibility warnings.
The required product outcome is narrower: through Avibe's normal backend call
path, the model is not shown the native Skill Catalog and is not offered the
native Skill tool.

## 10. Built-in Skills

### 10.1 Source and runtime mirror

Built-in Skills are maintained in the source tree at:

```text
skills/<name>/
```

At installation and upgrade, Avibe publishes a complete runtime mirror to:

```text
${AVIBE_HOME:-$HOME/.avibe}/builtin-skills/<name>/
```

The runtime directory is deliberately separate from user-managed Skills and
must be directly accessible to the agent so loaded built-ins can use their own
scripts and references.

### 10.2 Replacement lifecycle

The source tree is authoritative for each Avibe version. First run and every
upgrade perform an atomic full replacement of `builtin-skills`:

- new built-ins are added;
- changed built-ins are replaced; and
- built-ins removed from the release are deleted from the runtime mirror.

This guarantees that the installed built-in set exactly matches the running
Avibe version and never exposes a partially updated set. The directory is
Avibe-owned rather than a user customization surface.

## 11. Installation Defaults

The default user-level installation target is backend-neutral:

```text
$HOME/.agents/skills/<name>
```

An explicitly project-scoped installation targets:

```text
$PROJECT_ROOT/.agents/skills/<name>
```

Installing a Skill does not need to copy it into each backend's native
directory. Existing native directories remain discovery inputs for backward
compatibility.

Skill editing, adoption, and copy-on-write are outside v1. This protocol owns
runtime discovery and loading, while the existing Workbench/askill management
surface owns installation UI and package operations. That surface must adopt
these default targets when the runtime implementation lands.

## 12. System Prompt Slimming

Managed Skills make it possible to remove long, conditional operating manuals
from the always-on system prompt.

The kernel keeps only information required on every Turn:

- agent identity and Session facts;
- interaction and output contracts;
- permission and safety boundaries; and
- Skill discovery and loading instructions.

Long operational playbooks such as Show Pages, Vault, Harness, and Memory can
move to Avibe built-in Skills. A short routing invariant may remain in the
kernel when the agent must know to load a Skill before it can discover the
relevant workflow.

Prompt slimming is incremental. A module moves only after its built-in Skill
has equivalent behavior and regression coverage.

## 13. Implementation Ownership

One shared resolver owns discovery, loose parsing, precedence, deduplication,
sorting, pagination, and lookup. Prompt rendering and both CLI commands consume
that resolver rather than reimplementing any rule.

Backend adapters own only their native isolation setting and the mechanics for
applying a changed Avibe prompt to the same backend Session. Built-in
installation owns only the versioned source-to-runtime mirror.

The existing [Workbench Skills design](workbench-skills-page.md) remains the
management surface. This document supersedes only assumptions in that design
about installing one copy per backend or using backend-native Skill catalogs
at runtime.

## 14. Acceptance Criteria

### 14.1 Resolver and protocol

- A fixture covering every discovery root resolves one entry per final name.
- Built-in, project/global, directory depth, and directory-family precedence
  match Section 5.
- A Skill with extractable `name` and `description` is accepted despite
  unrelated malformed or unknown frontmatter.
- Invalid candidates do not prevent valid candidates from appearing.
- Prompt and `vibe skill list` pagination are stable, limited to 25 entries,
  and do not expose paths or sources.
- `vibe skill load` emits the exact XML wrapper, body only, and an absolute
  directory from which supporting files can be read.

### 14.2 Live Session behavior

For each supported backend, using one existing Avibe Session:

- installing a Skill makes it available on the next actual Turn;
- changing its name or description changes the next Turn's Catalog;
- changing its body changes the next load;
- deleting it removes it from the next Turn's Catalog; and
- none of these operations requires a restart or new Session.

Previously loaded content remains in conversation history without forcing a
reload or rewriting the Session.

### 14.3 Backend isolation

Capture the actual model-facing request for Claude Code, Codex, and OpenCode
and verify:

- the Avibe Catalog is equivalent across all three backends;
- the native backend Catalog is absent;
- OpenCode's native Skill tool is disabled; and
- each adapter retains the same native Session when the Catalog changes.

Codex `$skill`, backend TUI commands, and ordinary filesystem reads are not v1
isolation acceptance gates.

### 14.4 Built-in lifecycle

- a fresh installation mirrors all bundled Skills;
- an upgrade replaces changed Skills and removes retired Skills;
- replacement is atomic; and
- every loaded built-in reports an agent-accessible absolute directory.

## 15. Explicit Non-Goals

V1 does not include:

- unified global or project prompt files;
- disabling native `AGENTS.md` or `CLAUDE.md` loading;
- a new Skill compatibility schema;
- source namespaces or source disclosure to the agent;
- semantic ranking or filtering of Skills;
- Skill editing or copy-on-write;
- semantics for `${AVIBE_HOME:-$HOME/.avibe}/skills`;
- live filesystem watchers or historical-context invalidation; or
- a security sandbox preventing direct filesystem access.
