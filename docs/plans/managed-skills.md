# Avibe Managed Skills v1

> **Status:** Accepted
> **Date:** 2026-09-02
> **Scope:** Unified Skill discovery, Catalog injection, loading, backend
> isolation, and built-in lifecycle for Claude Code, Codex, and OpenCode

## 1. Product Decision

Avibe owns one Skill experience across every supported backend:

1. Users install or keep a Skill in any supported location.
2. Avibe discovers those locations and resolves one entry per Skill name.
3. Every backend receives the same name-and-description Catalog.
4. The agent loads a Skill with `vibe skill load -- <name>`.

The agent does not need to know which backend or directory contributed a
Catalog entry. The selected absolute directory is revealed only when the Skill
is loaded, so the Skill can use companion scripts, references, and assets.

Avibe disables backend-native Skill presentation on the backend calls it owns.
This creates one product-level route, not a filesystem sandbox: an agent that
already knows a path can still read it with ordinary filesystem tools.

## 2. Why v1 Starts With Skills

Backend prompt files are not yet a portable unit. Their paths, traversal rules,
and automatic injection differ, and not every backend provides a dependable
way to disable them. Avibe therefore does not unify `AGENTS.md`, `CLAUDE.md`,
or global system prompts in v1.

A Skill already has a compatible storage shape:

```text
example-skill/
|-- SKILL.md
|-- scripts/
|-- references/
`-- assets/
```

Avibe keeps this shape and replaces only discovery, Catalog presentation, and
loading in the Avibe runtime path.

## 3. Compatibility Contract

A candidate is a direct child directory containing `SKILL.md`. Nested files
remain part of that Skill and are not discovered as separate Skills.

The leading frontmatter needs only `name` and `description`:

```yaml
---
name: example-skill
description: Explain when this Skill should be used.
---
```

Parsing is permissive. Unknown or malformed optional fields do not reject an
otherwise usable Skill. The name must follow the portable Agent Skills token
shape: 1-64 lowercase ASCII letters, digits, or hyphens, with no leading,
trailing, or consecutive hyphen. Descriptions are normalized to one line for
Catalog display.

The existing optional `disable-model-invocation: true` field remains
meaningful. The winning Skill is resolved before this policy is applied: a
manual-only winner is omitted from Catalogs but remains available through an
explicit exact-name load. All other optional backend metadata is ignored by
the portable v1 execution path.

Invalid or unreadable candidates are omitted independently. Discovery and load
use bounded input and output so one compatibility directory or file cannot
make a Turn unbounded. Exact resource ceilings and race-resistant filesystem
mechanics are implementation safeguards owned by code and tests, not additions
to the model-facing protocol.

## 4. Discovery Sources

### 4.1 Avibe built-ins

Built-ins are maintained in the repository at:

```text
skills/<name>/
```

An installed Avibe artifact publishes its complete built-in set beneath:

```text
${AVIBE_HOME:-$HOME/.avibe}/builtin-skills/<runtime-snapshot>/<name>/
```

The runtime snapshot is directly readable by the agent. It is separate from
user-managed Skills and has the highest resolution priority.

### 4.2 Project compatibility roots

From the active working directory up to the project boundary, Avibe inspects:

```text
<directory>/.agents/skills
<directory>/.codex/skills
<directory>/.claude/skills
<directory>/.opencode/skills
```

The nearest matching directory wins. A Workbench Session uses its bound Avibe
project base as the boundary; a standalone command uses the first Git root, or
only its current directory when no Git root exists.

### 4.3 Global compatibility roots

Avibe inspects:

```text
$HOME/.agents/skills
${CODEX_HOME:-$HOME/.codex}/skills
${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills
${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills
${CODEX_HOME:-$HOME/.codex}/skills/.system
```

When Claude reports enabled plugins through its supported CLI, their standard
`skills` directories are also best-effort compatibility roots. Plugin lookup
failure omits only plugin roots; it never prevents static discovery or a
backend Turn.

Native locations are read in place. Avibe does not migrate, rename, annotate,
or expose their source identity to the agent.

### 4.4 Reserved root

```text
${AVIBE_HOME:-$HOME/.avibe}/skills
```

This path is reserved for a future user-level Avibe role. V1 does not scan it,
write to it, or assign it behavior.

## 5. Resolution and Precedence

Candidates are deduplicated by their declared `name`. Lower numbers win:

| Priority | Scope or family |
| --- | --- |
| 1 | Avibe built-ins |
| 2 | Project Skills, nearest directory first |
| 3 | Global Skills |

Within the same project depth or global scope, family order is:

```text
.avibe > .agents > .codex > .claude > .config/OpenCode
```

The `.avibe` family slot is reserved in v1. Enabled Claude plugin roots follow
the static user roots, and Codex `.system` defaults are last. A final stable
path order breaks any remaining tie. Winners are sorted by name before Catalog
pagination.

The agent sees only each winning `name` and `description`. It never sees source
namespaces, scopes, duplicate candidates, priority details, or compatibility
status.

## 6. Skill Catalog Protocol

### 6.1 System prompt injection

When model-invocable Skills exist, Avibe injects this shape:

```md
## Skills

Skills provide specialized instructions and workflows for specific tasks.
When a task matches a skill's description, run `vibe skill load -- <name>` before proceeding.
If the user requests a skill by exact name, load that name directly.
Otherwise, only load skill names listed here or returned by `vibe skill list`; do not guess names.
Use `vibe skill list --page 2` only when more discovery is useful; ordinary tasks do not require scanning every page.

### Available skills
- data-analysis: Analyze datasets, generate charts, and create reports.
- pdf-processing: Extract text, fill forms, and merge PDF files.
```

Page 1 contains at most 25 entries. The optional page-2 guidance appears only
when another page exists. If more entries exist, append:

```md
More skills are available. Run `vibe skill list --page 2` to view more.
```

When only manual-only Skills resolve, Avibe reveals no names or descriptions
and retains only exact-name guidance:

```md
## Skills

If the user requests a skill by exact name, run `vibe skill load -- <name>` before proceeding.
Otherwise, do not guess skill names.
```

When no Skills resolve, Avibe omits the block.

### 6.2 List command

```text
vibe skill list
vibe skill list --page <N>
```

The default is page 1. Each page contains at most 25 entries in stable name
order and prints only Catalog rows:

```text
- data-analysis: Analyze datasets, generate charts, and create reports.
- pdf-processing: Extract text, fill forms, and merge PDF files.
```

When another page exists, the output ends with the next-page sentence using
the appropriate page number. Pagination is live: after changing Skills while
paging, callers restart at page 1 rather than relying on a hidden snapshot.

## 7. Skill Load Protocol

Both forms are accepted; agent instructions use the second:

```text
vibe skill load <name>
vibe skill load -- <name>
```

A successful load writes exactly one wrapper to standard output:

```xml
<skill_content name="pdf-processing" directory="/absolute/path/to/pdf-processing">
SKILL BODY ONLY
</skill_content>
```

Contract:

- `name` is the resolved portable Skill name.
- `directory` is the absolute, agent-accessible directory containing
  `SKILL.md`; XML attribute characters are escaped.
- The payload is only the body after the leading frontmatter.
- The body is otherwise unchanged. There is no generated title, summary,
  indentation, JSON envelope, protocol header, source label, or file manifest.
- Relative references to scripts, references, assets, and other companion
  files resolve from `directory`.
- Load resolves the current winner from disk and verifies the selected file
  still declares the requested name before it emits content.

On failure, standard output is empty, standard error contains a short
human-readable error, and the process exits non-zero.

The wrapper is model-facing framing, not a parsed XML document. A loaded Skill
is tool-level context and cannot override Avibe's system prompt, permissions,
or safety rules.

## 8. Freshness and Session Semantics

Immediately before every new Turn that Avibe dispatches, it resolves the live
Catalog and renders the current system prompt. The existing backend Session is
retained.

Therefore, without restarting Avibe or creating a new Session:

- installing a Skill makes it available on the next Turn;
- renaming it or changing its description updates the next Catalog;
- changing its body updates the next load; and
- deleting it removes it from the next Catalog and load.

`vibe skill list` and `vibe skill load` also resolve from disk on every
invocation. Backend-bound commands use the same Session working directory,
project boundary, compatibility homes, and built-in snapshot that produced the
Turn's Catalog. Binding is internal runtime state, not prompt text or a command
argument.

Binding infrastructure is auxiliary to backend execution. If it is temporarily
unavailable, Avibe keeps the Turn usable and withholds any Catalog whose load
address it cannot bind; later Turns recover automatically when binding succeeds.

A message steered into an already active backend Turn is part of that Turn and
does not cause another refresh. A backend-native continuation that Avibe did
not dispatch may retain its existing prompt snapshot. Both cases leave the next
Avibe-dispatched Turn guarantee unchanged.

Previously loaded bodies remain ordinary conversation history. Avibe does not
rewrite history, invalidate old content, track loaded revisions, or instruct
the agent to distrust earlier loads.

V1 uses no filesystem watcher, change notification, incremental prompt patch,
or long-lived Catalog cache. Turn-time rendering and live commands are the
consistency boundaries.

## 9. Backend Isolation

| Backend | Native isolation | Avibe prompt application |
| --- | --- | --- |
| Claude Code | Configure the SDK with `skills=[]`. | Rebuild when the Skill Catalog changes and resume the same native Session. |
| Codex | Set `skills.include_instructions=false`. | Render `developerInstructions` for each Turn while retaining the same thread. |
| OpenCode | Send `tools.skill=false` in every prompt request. | Send the current system prompt on every new Turn. |

For OpenCode, `tools.skill=false` is a request parameter, not prompt text, and
Avibe does not rewrite the user's permission configuration.

V1 does not block handwritten Codex `$skill` references, backend TUI or slash
commands Avibe does not call, or ordinary filesystem access. The outcome is
that Avibe's normal backend calls expose only the Avibe Catalog and load route.

## 10. Built-in Lifecycle

The bundled `skills/` tree is authoritative for each Avibe artifact. Before
managed discovery, the artifact publishes a complete, agent-readable runtime
snapshot. A new version therefore adds new built-ins, replaces changed ones,
and omits retired ones as one coherent set.

Publication is atomic and content-addressed so overlapping old and new Avibe
processes can keep using the snapshot each one advertised. Published snapshots
are not a user customization surface. V1 does not garbage-collect old
snapshots, detect later manual tampering, or repair user changes there.

Wheels and source distributions include the authoritative built-in tree and
preserve companion files and executable script modes. A publication failure
omits built-ins without substituting a different version's content.

## 11. Installation Defaults

The default user-level target is backend-neutral:

```text
$HOME/.agents/skills/<name>
```

An explicitly project-scoped install targets:

```text
$PROJECT_BASE/.agents/skills/<name>
```

The Workbench presents one logical installation, not a backend selector. It
may keep backend-native compatibility links required by existing tooling, but
Avibe still resolves and presents one Skill. Existing native installs stay in
place and remain compatibility inputs.

Skill editing, adoption, and copy-on-write are outside v1.

## 12. Prompt Slimming

Managed Skills allow long, conditional operating manuals to leave the always-on
system prompt. The kernel keeps only identity, interaction contracts,
permissions, safety boundaries, and the short Skill routing instructions.

Operational modules move incrementally into Avibe built-ins only after the
replacement Skill has equivalent behavior and regression coverage.

## 13. Ownership

One shared resolver owns discovery, permissive parsing, precedence,
deduplication, pagination, and lookup. Prompt rendering and both CLI commands
consume it.

Backend adapters own only native isolation, Session-bound command context, and
application of a changed Avibe prompt. Built-in publication owns only the
artifact-to-runtime snapshot lifecycle.

The existing [Workbench Skills design](workbench-skills-page.md) remains the
management surface. This protocol supersedes its backend selection and
per-backend runtime availability model.

## 14. Acceptance

Automated and local Incus regression must prove:

- deterministic discovery and the declared precedence across compatibility
  roots, built-ins, project scope, global scope, duplicates, and invalid input;
- the exact Catalog, pagination, and load output contracts;
- body-only load plus direct access to companion scripts and references;
- live add, edit, rename, and delete behavior in one existing Session;
- equivalent Catalogs and native isolation across Claude, Codex, and OpenCode;
- installation and upgrade publication of complete built-in snapshots; and
- bounded failure of malformed, oversized, unavailable, or concurrently
  changing inputs without taking down unrelated Skills or backend Turns.

## 15. Non-Goals

V1 does not include:

- unified global or project prompt files;
- disabling native `AGENTS.md` or `CLAUDE.md` loading;
- an Avibe-specific compatibility schema or source namespace;
- semantic ranking beyond deterministic precedence;
- Skill editing or copy-on-write;
- behavior for `${AVIBE_HOME:-$HOME/.avibe}/skills`;
- historical-context invalidation;
- built-in snapshot garbage collection or self-healing; or
- a filesystem security sandbox.
