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

Avibe requires only two fields in the leading frontmatter:

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
requirement. Avibe reuses the portable Agent Skills name grammar rather than
inventing another identifier format; all other frontmatter remains optional.

## 4. Discovery

### 4.1 Candidate shape

A discovery root contributes only direct child Skill directories:

```text
<discovery-root>/<skill-directory>/SKILL.md
```

Avibe does not recursively treat arbitrary nested `SKILL.md` files as separate
Skills. Directories such as `scripts/`, `references/`, and `assets/` remain
part of their parent Skill.

Avibe opens `SKILL.md` with platform nonblocking semantics, immediately uses
`fstat` or the platform-equivalent handle query to verify that same open handle
is a regular file, and performs the bounded parse or load through that handle.
It performs no potentially blocking read before handle-level type verification.
A path-level check alone never authorizes a read, so replacing a candidate
between enumeration and open cannot redirect the checked operation to a FIFO,
socket, device, directory, broken link, or other non-regular target. Load
repeats the same open-handle contract.

Discovery and load share one verified-read primitive. It records the open
file's identity, size, high-resolution modification time, and change/version
metadata available on the platform before the bounded read, queries the same
handle afterward, and accepts the bytes only when every recorded value remains
unchanged. An in-place rewrite, truncation, or replacement during the read
therefore omits the candidate during discovery or fails load with empty standard
output. This consistency rule is owned once by the resolver rather than
reimplemented by Catalog and load call sites.

Backend-bundled or system Skills, plugin caches, administrator-only Skills,
and `.claude/commands` are not discovery sources.

### 4.2 Built-in root

```text
${AVIBE_HOME:-$HOME/.avibe}/builtin-skills/<snapshot-id>
```

This is logical user-facing notation. Implementations resolve it through
`config.paths.get_vibe_remote_dir()` so a legacy `~/.vibe_remote` home remains
authoritative when the compatibility migration cannot move it. Each running
Avibe artifact selects only the version-scoped snapshot derived from its own
bundled Skill tree. The `builtin-skills` umbrella is not a generic discovery
root for direct child Skills. The selected snapshot is Avibe-owned runtime
content, has the highest resolution priority, and contains at most 1,024 direct
child Skill directories whose bounded frontmatter totals at most 8 MiB. This
keeps every published built-in discoverable within the same candidate and byte
budgets as compatibility inputs.

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
${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills
${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills
```

Native locations are compatibility inputs. Avibe reads them in place and does
not migrate, rename, or annotate their Skills. Implementations resolve the
Claude root through the existing `vibe.claude_config.get_claude_home()` helper
so discovery and the live Claude backend honor the same override semantics.

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
- accept the Skill when `description` is non-empty and `name` follows the
  portable Agent Skills grammar;
- ignore every other field;
- do not reject a Skill because unrelated frontmatter is unknown or malformed;
- do not require the declared name to match the directory name; and
- fold whitespace in `description` into a single-line Catalog value.

If either required value cannot be extracted, omit that candidate without
failing the whole Catalog. The name grammar is the existing Agent Skills
boundary: 1-64 lowercase ASCII letters, digits, or hyphens; no leading,
trailing, or consecutive hyphen. This makes every advertised name one literal
shell token without Avibe-specific quoting or encoding.

Catalog parsing and rendering are bounded independently:

- read at most 64 KiB of leading frontmatter, including its delimiters, and
  omit the candidate if the closing delimiter is not found within that budget;
- accept a body of at most 256 KiB of encoded bytes, measured from the closing
  frontmatter delimiter to end of file without reading the body during Catalog
  discovery;
- truncate a normalized description beyond 1,024 characters, adding `...`;
- render at most 25 entries and 16 KiB of Skill rows per page.

The bounded read happens before decoding or normalizing field values, so a
large optional field, description, or unterminated frontmatter cannot create
unbounded per-Turn work. Discovery enumerates at most 1,025 direct child
entries in any root and omits the entire root if it contains more than 1,024.
One resolution gives the built-in root and the combined project/global roots
independent aggregate budgets of 1,024 candidate Skill directories and 8 MiB
of frontmatter bytes each. A full built-in root therefore cannot consume the
capacity reserved for user Skills. Within the compatibility-input budget,
roots are visited in precedence order and each accepted root's candidates in
stable name order. Reaching that budget omits the remaining lower-priority
roots. These rules make omission deterministic without walking the rest of an
oversized directory. Omission is diagnostic log data, not additional prompt
content.

These limits do not validate optional frontmatter, require the directory and
declared name to match, or add compatibility metadata.

### 5.2 Deduplication

Candidates are deduplicated by their final declared `name`. The winner is
selected by these rules, in order:

1. Avibe built-ins over project and global Skills.
2. Project Skills over global Skills.
3. Within project scope, a directory nearer to the working directory over a
   more distant directory.
4. At the same scope and depth, directory-family priority is:
   `.avibe` > `.agents` > `.codex` > `.claude` > OpenCode.
5. If every preceding dimension ties, the candidate whose absolute directory
   path sorts first by Unicode code-point order wins.

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
When a task matches a skill's description, run `vibe skill load -- <name>` before proceeding.
If the user requests a skill by name, load it.
Only load skill names listed here or returned by `vibe skill list`; do not guess names.

### Available skills
- data-analysis: Analyze datasets, generate charts, and create reports.
- pdf-processing: Extract text, fill forms, and merge PDF files.
```

The system prompt contains page 1 only, within the entry and row budgets in
Section 5.1. If more entries exist, append exactly:

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
vibe skill load -- <name>
```

Both forms are accepted. The second is the canonical form emitted in agent
instructions; portable names cannot begin with an option prefix, but the
end-of-options marker keeps the command contract explicit.

The command resolves live global and project roots together with the caller's
bound Session working directory and built-in snapshot, selects the current
winner for `name`, and writes this structure to standard output. A bound
command never derives project scope from the shell command's own current
directory:

```xml
<skill_content name="pdf-processing" directory="/absolute/path/to/pdf-processing">
SKILL BODY ONLY
</skill_content>
```

Contract details:

- `name` is the resolved Skill name.
- `directory` is the absolute, agent-accessible directory containing
  `SKILL.md`.
- Load retains an open handle to the selected Skill directory, reads
  `SKILL.md` relative to that handle, and verifies immediately before output
  that the reported absolute path still names the same directory identity. If
  the directory moved, disappeared, or was replaced during load, the command
  fails instead of pairing one body with another directory.
- The required frontmatter and bounded body are parsed from one verified
  `SKILL.md` read after selection. Load confirms that those exact bytes still
  declare the requested portable name before emitting; it never combines a
  name retained from an earlier discovery read with newly opened content.
- XML attribute values are escaped.
- Only the body after the leading frontmatter is emitted.
- The body is otherwise unchanged: no generated title, indentation, escaping,
  summary, or rewrite is added.
- A body larger than 256 KiB is not advertised and cannot be loaded. If a file
  grows beyond the limit between discovery and load, load fails with empty
  standard output rather than truncating it.
- Relative references to `scripts/`, `references/`, `assets/`, or other files
  resolve from `directory`.
- The directory-identity check covers the load operation; it does not make a
  mutable user Skill immutable after the command returns. Later edits follow
  ordinary filesystem semantics and become visible to later reads or loads.
- There is no protocol/version header, JSON envelope, source label,
  compatibility state, or file manifest.

On failure, standard output is empty, standard error contains a short
human-readable error, and the process exits non-zero.

The wrapper is XML-like model-facing framing, not an XML document consumed by
an XML parser. Only its attributes use XML escaping; the Markdown body is an
opaque, unchanged payload and may itself contain XML-looking text. The wrapper
does not change instruction precedence. A loaded Skill is tool-level context
and cannot override Avibe's system prompt, permissions, or safety rules.

## 8. Freshness and Session Semantics

### 8.1 Avibe-dispatched Turn snapshot

Immediately before each new Turn that Avibe dispatches to a backend, Avibe:

1. resolves the Catalog from the current filesystem state;
2. renders the current system prompt with page 1 of that Catalog; and
3. dispatches the Turn to the existing backend Session.

This gives an existing Avibe Session newly installed, renamed, edited, or
removed Skills on its next Avibe-dispatched Turn. No Avibe restart and no new
Session are required.

A message steered into a backend Turn that is already active is part of that
same Turn and does not trigger another Catalog refresh. Changes become visible
on the next Avibe-dispatched Turn.

A backend-native continuation that starts without an Avibe dispatch, such as a
Claude background-tool completion or native wakeup, cannot be intercepted
before it begins and may use the prompt snapshot already held by that backend
client. It does not weaken the next Avibe-dispatched Turn guarantee and is not
part of the v1 freshness contract.

### 8.2 Live commands

`vibe skill list` and `vibe skill load` resolve from disk on every invocation.
Therefore:

- adding or removing a Skill changes the next command result and next Turn;
- changing `name` or `description` changes the next Catalog resolution; and
- changing the body, scripts, or references changes the next load or file
  read.

When a backend launches either command, Avibe supplies two internal bindings:

- `AVIBE_SKILL_WORKING_DIR` is the absolute working directory from which Avibe
  rendered that Session's Catalog. Project discovery always starts there, even
  when the agent runs the command from another directory.
- `AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID` selects the version-scoped built-in snapshot
  retained by the Avibe process that created that backend runtime, even if an
  upgrade has since switched the stable `vibe` launcher to another artifact.

These values use the existing per-Session shell-environment path for each
backend: Claude SDK process environment, Codex `shell_environment_policy`, and
the OpenCode caller-context plugin. They are not prompt text or agent-visible
command arguments. A standalone command without Avibe bindings uses its own
current working directory and the snapshot bundled with its own executable.

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

The initial implementation scans the fixed roots within the candidate and byte
budgets and reads only enough of each `SKILL.md` to extract frontmatter. A
reference measurement of six roots, 20 Skill files, and 242 KB total input
completed in about 1 ms median on a warm local filesystem. This is not a
latency guarantee; it shows that correctness can precede caching.

V1 does not cache discovery results. A future parsed-frontmatter cache cannot
treat file identity, size, or timestamps as proof that content is unchanged: it
must read and digest the bounded frontmatter on that Turn before reusing a
parsed value. Any optimization must preserve the same freshness contract.

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

Release artifacts must carry that authoritative tree: wheels force-include it
under a package-owned resource path and sdists include `skills/**`. Runtime
code resolves the checkout path during development and the packaged resource
path after installation; it never assumes the repository root exists in a
wheel environment. Built-in trees contain only directories and regular files;
release packaging and publication preserve whether each file is executable.

Before the first managed Skill resolution from an installed or upgraded Avibe
artifact, Avibe publishes a complete runtime snapshot to:

```text
${AVIBE_HOME:-$HOME/.avibe}/builtin-skills/<snapshot-id>/<name>/
```

`<snapshot-id>` is lowercase SHA-256 over an unambiguous snapshot-v1 byte
stream. The stream begins with the ASCII domain separator
`avibe-builtin-snapshot-v1` followed by NUL. It then contains one record for
every directory except the source root and every regular file, ordered by the
UTF-8 bytes of its `/`-separated relative path. A record contains a one-byte
directory/file tag, the path length as an unsigned 64-bit big-endian integer,
and the path bytes. A file record additionally contains its byte length in the
same integer encoding, its exact bytes, and one byte holding
`st_mode & 0o111` where POSIX executable bits exist (zero otherwise). Release
packaging requires relative paths to be valid UTF-8 in NFC form. A fixed
tree-to-digest fixture freezes this encoding without creating a persisted
manifest. The identifier selects a directory; it is not a runtime integrity
protocol.

Built-in source paths must be representable without aliases on every supported
platform. Release packaging rejects absolute or traversal paths, backslashes,
NUL, drive/UNC prefixes, Windows-reserved components, trailing-dot/space names,
and case-insensitive path collisions. It also rejects a 1,025th direct child
Skill directory or a built-in tree whose bounded frontmatter exceeds 8 MiB.

The runtime directory is deliberately separate from user-managed Skills and
is directly accessible to the agent so loaded built-ins can use their own
scripts and references.

### 10.2 Replacement lifecycle

The source tree is authoritative for each Avibe artifact. Each snapshot is a
complete mirror that Avibe itself never mutates:

- new built-ins are added;
- changed built-ins are replaced; and
- built-ins removed from the artifact are absent from its snapshot.

Publication is serialized by a cross-process lock keyed by snapshot digest.
The publisher builds and validates a hidden sibling staging directory, then
atomically renames it once into the previously absent digest path. A process
interruption can leave only an undiscoverable staging directory. If the digest
path already exists, concurrent or later publishers reuse that path without
mutating it. A wrong-type or unreadable path fails safely. Readable external
changes are unsupported post-publication mutation and may affect later
Catalog/load results; runtime commands neither validate nor repair the path
from whichever artifact the stable launcher currently selects.

After publication, list/load does not hash the full snapshot, compare it with a
package source, or repair it. It reads the selected built-in through the same
bounded verified-file contract as every other Skill. The snapshot is an
Avibe-owned lifecycle surface, not a user customization or security boundary;
direct post-publication mutation is unsupported and may change or omit a later
Catalog/load result.

Every resolver selects the digest computed from the bundled source of its own
running artifact. Concurrent old and new Avibe processes therefore use
different snapshots when their built-ins differ, while identical trees safely
share one. Other snapshots are not discovery candidates. V1 retains them so a
running older process and an already loaded Skill directory remain usable;
garbage collection of unreferenced snapshots is outside this protocol.

Backend runtimes inherit this selected digest as
`AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID`, so `vibe skill list` and
`vibe skill load` remain bound to the advertising runtime's built-ins across an
overlapping upgrade. The digest is validated as an identifier under the
`builtin-skills` umbrella before use; it cannot select an arbitrary path. A
newer stable launcher needs only that retained path, not the older package
source. A missing or unreadable retained snapshot fails safely instead of
falling back to the newer artifact's built-ins.

## 11. Installation Defaults

The default user-level installation target is backend-neutral:

```text
$HOME/.agents/skills/<name>
```

An explicitly project-scoped installation targets:

```text
$PROJECT_BASE/.agents/skills/<name>
```

`$PROJECT_BASE` is the Git project root when one exists and the active working
directory otherwise. This matches the project-scope fallback used by
discovery.

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
- Global-root fixtures override `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and
  `XDG_CONFIG_HOME` and resolve from the same homes as their live backends.
- Built-in, project/global, directory depth, and directory-family precedence
  match Section 5.
- Two sibling directories declaring the same name resolve by the final absolute
  path tie-breaker, independent of filesystem enumeration order.
- A Skill with a portable name and extractable description is accepted despite
  unrelated malformed or unknown frontmatter.
- Invalid candidates do not prevent valid candidates from appearing.
- A fixture that replaces a regular `SKILL.md` with a FIFO between enumeration
  and open cannot block discovery: handle-level type validation omits it before
  any read, and load follows the same descriptor-bound contract.
- An in-place rewrite, truncation, or replacement of `SKILL.md` during the
  verified read is never accepted: discovery omits it and load exits non-zero
  with empty standard output.
- Frontmatter parsing reads no more than 64 KiB before accepting or omitting a
  candidate, including for oversized or unterminated input.
- A root with more than 1,024 direct children is omitted after enumerating at
  most 1,025 entries. Built-ins and combined project/global compatibility
  inputs each have a separate 1,024-candidate and 8 MiB frontmatter budget, so
  either class can exhaust its own budget without consuming the other's.
- Prompt and `vibe skill list` pagination are stable, limited to 25 entries,
  remain within the row budget, and do not expose paths or sources.
- Oversized descriptions cannot make the Catalog unbounded, and names with
  whitespace, shell syntax, uppercase characters, or invalid hyphen placement
  are omitted by the parser-backed portable name boundary.
- `vibe skill load` emits the exact XML wrapper, body only, and an absolute
  directory from which supporting files can be read.
- Parser-backed CLI coverage dispatches the canonical
  `vibe skill load -- pdf-processing` example through the real argument parser
  and reaches the load handler with `pdf-processing` as its single name.
- If the selected `SKILL.md` is replaced after discovery, load reparses the
  exact verified bytes it will emit and fails unless they still declare the
  requested name.
- A backend-bound load launched from a different shell directory still uses
  the Session working directory that produced the advertised Catalog.
- Replacing, removing, or renaming the selected Skill directory between
  selection and output produces empty standard output and a non-zero exit; it
  cannot pair the opened body with a different directory identity.
- A body over 256 KiB is omitted from discovery, and a body that crosses the
  limit before load produces empty standard output and a non-zero exit.

### 14.2 Live Session behavior

For each supported backend, using one existing Avibe Session and
Avibe-dispatched Turns:

- installing a Skill makes it available on the next Avibe-dispatched Turn;
- changing its name or description changes the next Avibe-dispatched Turn's
  Catalog;
- changing its body changes the next load;
- deleting it removes it from the next Avibe-dispatched Turn's Catalog; and
- none of these operations requires a restart or new Session.

Previously loaded content remains in conversation history without forcing a
reload or rewriting the Session. Backend-native continuations that Avibe does
not dispatch are outside this acceptance boundary.

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
- a real wheel-install fixture proves bundled Skills exist without a source
  tree and preserves executable modes required by helper scripts;
- a fixed snapshot-v1 fixture proves the canonical tree byte stream and
  lowercase SHA-256 identifier remain stable;
- release packaging rejects non-portable, Windows-reserved, trailing-dot/space,
  or case-insensitively colliding built-in paths, a 1,025th direct Skill, and a
  tree whose bounded frontmatter exceeds the built-in 8 MiB budget;
- a mode-only built-in change produces a different snapshot and published
  digest;
- an upgrade's selected snapshot contains changed Skills and omits retired
  Skills;
- interrupted publication cannot expose a partial snapshot;
- a wrong-type or unreadable pre-existing digest path fails safely without
  being traversed, mutated, or rebuilt by runtime commands;
- two concurrently running artifacts with different bundled trees resolve
  different version-scoped snapshots;
- after launcher activation, a command inherited from the older runtime still
  lists and loads that runtime's retained built-in snapshot path without the
  older package source and never falls back to the newer snapshot; and
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
- garbage collection of old built-in snapshots;
- post-publication tamper detection or self-healing for Avibe-owned built-in
  snapshots;
- live filesystem watchers or historical-context invalidation; or
- a security sandbox preventing direct filesystem access.
