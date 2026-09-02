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
part of their parent Skill. The only nested container exception is the
explicit Codex `.system` discovery root in Section 4.4; its direct children
still follow the same candidate shape.

Compatibility roots accept either a direct child directory or a direct child
directory symlink. A symlink contributes the resolved target as the directory
shown by `vibe skill load`; discovery and load revalidate both the alias and
target identities so replacing either cannot pair one Skill body with another
directory. Avibe-owned built-in snapshots remain symlink-free.

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
unchanged. This detects changes observable through the host filesystem; on a
filesystem whose metadata cannot distinguish a same-size concurrent rewrite,
consistency is best-effort rather than a locking or immutable-snapshot
guarantee. Before accepting, it also queries the `SKILL.md` directory entry
again, relative to the retained directory handle when one is available, and
requires that path to remain a regular file naming the same file identity as
the open handle. An observable in-place rewrite, truncation, or atomic path
replacement during the read therefore omits the candidate during discovery or
fails load
with empty standard output. This consistency rule is owned once by the resolver
rather than reimplemented by Catalog and load call sites.

Backend-bundled or system Skills other than the explicitly listed Codex
`.system` compatibility root and enabled Claude plugin roots are not discovery
sources. Avibe does not crawl plugin caches or marketplaces, import
administrator-only Skills, or treat `.claude/commands` as Skills.

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
content, has the highest resolution priority, and contains at most 1,024 total
direct child entries, each a valid Skill directory, whose bounded frontmatter
totals at most 8 MiB. Every closing frontmatter delimiter is within the 64 KiB
per-candidate bound and every body is at most 256 KiB. This keeps every
published built-in discoverable within the same root, candidate, and byte
budgets as compatibility inputs.

### 4.3 Project roots

For each directory `D` from the active working directory up to and including
the first project boundary, Avibe inspects:

```text
<D>/.agents/skills
<D>/.codex/skills
<D>/.claude/skills
<D>/.opencode/skills
```

When a Session has a bound Avibe project base, that base is the project
boundary even if the Session works inside a nested Git checkout. The base is
snapshotted with the Session when the Session is created, so moving the Session
to another scope does not change which project-level Skills it sees. This also
lets a Workbench project whose configured directory has no `.git` marker retain
project-level Skills when an existing Session works in one of its descendants.
For a pre-upgrade Session without this snapshot, resolution safely derives the
current scope workdir when it is an ancestor; a later scope move first persists
that derived value. If an already-moved legacy row has no recoverable ancestor,
Avibe does not guess the former project base and uses the standalone boundary
rules.
A standalone command has no Avibe project binding and therefore uses the first
Git root; if neither boundary is found, only its active working directory is
project scope.

The nearest directory to the active working directory wins within the same
directory family. Root discovery examines at most 128 directories including
the active working directory and performs at most 512 project-root probes
before the existing candidate and byte budgets apply. A bound Avibe project
base is accepted only when it is an absolute ancestor reachable within those
128 directories; otherwise it is ignored and the standalone boundary rules
apply.

### 4.4 Global roots

```text
$HOME/.agents/skills
${CODEX_HOME:-$HOME/.codex}/skills
${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills
${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills
${CODEX_HOME:-$HOME/.codex}/skills/.system
```

Native locations are compatibility inputs. Avibe reads them in place and does
not migrate, rename, or annotate their Skills. Implementations resolve the
Claude root through the existing `vibe.claude_config.get_claude_home()` helper
so discovery and the live Claude backend honor the same override semantics.
Codex's `.system` directory is an explicit container root rather than a generic
recursive exception: Avibe inspects only its direct child Skill directories and
visits it after every user-managed global root, including enabled Claude plugin
roots. A same-name project or user global Skill therefore wins over the
Codex-bundled default. The container entry
counts toward the parent root's raw direct-child enumeration limit, but is not
a candidate and consumes no frontmatter budget there. Its children are charged
only when the explicit Codex system root is scanned.

If `${CLAUDE_CONFIG_DIR}/plugins/installed_plugins.json` exists, Avibe invokes
the official `<configured-claude-cli> plugin list --json` command in the Turn's
bound working directory and scans `<installPath>/skills` for each entry whose
`enabled` field is exactly `true`. The executable is the normalized V2
`agents.claude.cli_path` used by the live Claude backend, including a custom
path; standalone commands fall back to the normal Claude executable lookup. It
does not crawl the installation cache or trust stale registry entries directly.
Disabled plugins and relative installation paths are omitted. This lookup runs
on every Avibe-dispatched Turn so installing,
enabling, disabling, or removing a plugin is reflected without restarting Avibe
or creating a Session. A missing CLI, non-zero exit, timeout after one second,
combined standard output and standard error over 1 MiB, invalid UTF-8 or JSON,
or more than 256 reported entries omits plugin roots without failing the rest
of discovery. The combined output limit is enforced while both streams are
drained rather than after either stream has been buffered without a bound.

Enabled Claude plugin roots are compatibility inputs for the shared Avibe
Catalog, not a Claude-only feature. They are scanned after the four user static
roots and before Codex's bundled `.system` root, share the compatibility
aggregate budget, and expose only each portable Skill name and description.
Their plugin ID, installation path, and source are not shown to the agent.

### 4.5 Reserved root

```text
${AVIBE_HOME:-$HOME/.avibe}/skills
```

This path is reserved for a future user-level Avibe Skill role. In v1 Avibe
does not scan it, write to it, or assign it any behavior.

## 5. Parsing and Resolution

### 5.1 Loose frontmatter parsing

Discovery is deliberately permissive:

- extract `name`, `description`, and the optional existing
  `disable-model-invocation` policy from the leading frontmatter;
- accept the Skill when `description` is non-empty and `name` follows the
  portable Agent Skills grammar;
- ignore every other field;
- do not reject a Skill because unrelated frontmatter is unknown or malformed;
- do not require the declared name to match the directory name; and
- accept quoted or plain managed-field keys and decode standard YAML escapes
  in quoted keys and required values; and
- fold plain, quoted, or block `description` continuation lines and whitespace
  into a single-line Catalog value.

If either required value cannot be extracted, omit that candidate without
failing the whole Catalog. The name grammar is the existing Agent Skills
boundary: 1-64 lowercase ASCII letters, digits, or hyphens; no leading,
trailing, or consecutive hyphen. This makes every advertised name one literal
shell token without Avibe-specific quoting or encoding.
For valid YAML, a base loader composes at most 1,024 nodes and 128 alias
references, then reads only top-level scalar managed fields; it does not run
typed constructors or recursively materialize aliases. If composition is
malformed or exceeds either bound, a line scanner extracts only top-level
managed fields and ignores nested structures. Unrelated metadata therefore
cannot amplify work beyond the explicit bounds or abort Catalog construction.

An existing `disable-model-invocation: true` declaration remains manual-only:
the Skill is omitted from the injected Catalog and `vibe skill list`, but an
explicit `vibe skill load -- <name>` still resolves it. The field is optional;
Skills declaring only `name` and `description` remain automatically available.

Catalog parsing and rendering are bounded independently:

- read at most 64 KiB of leading frontmatter, including its delimiters, and
  omit the candidate if the closing delimiter is not found within that budget;
- accept a body of at most 256 KiB of encoded bytes, measured from the closing
  frontmatter delimiter to end of file without reading the body during Catalog
  discovery;
- truncate a normalized description beyond 1,024 characters, adding `...`;
- replace decoded Unicode control characters with spaces before collapsing
  whitespace, so Catalog output cannot carry terminal control sequences; and
- render at most 25 entries and 16 KiB of Skill rows per page.

The bounded read happens before decoding or normalizing field values, so a
large optional field, description, or unterminated frontmatter cannot create
unbounded per-Turn work. Discovery enumerates at most 1,025 direct child
entries in any root and omits the entire root if it contains more than 1,024.
One resolution gives the built-in root and the combined project/global roots
independent aggregate budgets of 4,096 direct child entries, 1,024 candidate
Skill directories, and 8 MiB of frontmatter bytes each. A full built-in root
therefore cannot consume the capacity reserved for user Skills. Within each
class, roots are visited in precedence order. Before reading frontmatter,
direct children of each root are sorted by their absolute entry path; declared
names participate only after parsing. Compatibility entries that resolve to the
same directory identity are visited once in precedence order and consume one
candidate and frontmatter slot; each alias remains charged to the direct-child
budget because it was enumerated. If a root would exceed the remaining
direct-child budget, Avibe observes at most one entry beyond that remainder,
omits the whole root, and omits all lower-priority roots. Reaching the candidate
or frontmatter budget likewise omits remaining lower-priority roots. These
rules make pre-parse work and omission deterministic without walking the rest
of an oversized directory. Omission is diagnostic log data, not additional
prompt content.

These limits do not validate optional frontmatter, require the directory and
declared name to match, or add compatibility metadata.
Catalog discovery also does not read the complete body merely to validate its
encoding. A successful load requires the bounded body to be valid UTF-8; an
invalid body can appear in the Catalog but load fails with no standard output.

### 5.2 Deduplication

Candidates are deduplicated by their final declared `name`. The winner is
selected by these rules, in order:

1. Avibe built-ins over project and global Skills.
2. Project Skills over global Skills.
3. Within project scope, a directory nearer to the working directory over a
   more distant directory.
4. At the same scope and depth, directory-family priority is:
   `.avibe` > `.agents` > `.codex` > `.claude` > OpenCode > enabled Claude
   plugins > Codex system.
5. If every preceding dimension ties, the candidate whose absolute directory
   path sorts first by Unicode code-point order wins.

The `.avibe` family reserves the highest family slot for future use, but
`${AVIBE_HOME:-$HOME/.avibe}/skills` is inactive in v1. OpenCode means
`.opencode` at project scope and `${XDG_CONFIG_HOME:-$HOME/.config}/opencode`
at global scope. Codex system means the explicit
`${CODEX_HOME:-$HOME/.codex}/skills/.system` container. Enabled Claude plugin
roots follow user static roots but precede Codex system defaults, so no bundled
default shadows an enabled user plugin.

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

When at least one model-invocable Skill is available, Avibe injects this block
with the resolved rows substituted:

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

The system prompt contains page 1 only, within the entry and row budgets in
Section 5.1. The optional subsequent-page guidance is present only when another
page exists. A user-requested exact name may be loaded directly without first
finding it in a paged Catalog. If more entries exist, append exactly:

```md
More skills are available. Run `vibe skill list --page 2` to view more.
```

When resolved Skills exist but every one declares
`disable-model-invocation: true`, Avibe omits every name and description but
retains only this exact-name loading guidance:

```md
## Skills

If the user requests a skill by exact name, run `vibe skill load -- <name>` before proceeding.
Otherwise, do not guess skill names.
```

This keeps an explicitly requested manual-only Skill usable without advertising
it for model selection. When no Skills of either kind resolve, Avibe omits the
entire block.

### 6.2 List command

```text
vibe skill list
vibe skill list --page <N>
```

The default page is page 1. Each page contains at most 25 entries in the same
stable name order used by the prompt for the filesystem state observed by that
invocation. Standard output uses only Catalog rows:

```text
- data-analysis: Analyze datasets, generate charts, and create reports.
- pdf-processing: Extract text, fill forms, and merge PDF files.
```

When another page exists, the output ends with the same next-page sentence
used by the prompt, with the appropriate page number. Paths, sources, scopes,
and resolution metadata are not printed.

Page boundaries are packed from the stable name order under both limits. Page
`N+1` starts with the first entry after the last row actually emitted on page
`N`; the 16 KiB limit never skips entries that did not fit an earlier page.

Pagination is deliberately live rather than a cross-command snapshot. If the
filesystem changes between `list --page` invocations, entries can move between
pages; after installing, editing, renaming, or deleting a Skill while paging,
the caller restarts at page 1. V1 does not add a hidden per-Session cursor or
freeze the Catalog merely to linearize an uncommon concurrent scan. With an
unchanged filesystem, every invocation produces the same order and page
boundaries.

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
  `SKILL.md`. Compatibility candidates whose resolved absolute directory is
  not valid UTF-8 are omitted; v1 does not invent a second path encoding for
  model-facing output. After ordinary XML attribute escaping, every Unicode
  control character in the path is emitted as an ASCII numeric character
  reference such as `&#xA;`, so the command prints no raw terminal controls and
  the represented path remains reversible.
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
- On a successful load, the body is otherwise unchanged: no generated title,
  indentation, escaping, summary, or rewrite is added. Invalid UTF-8 or a C0/C1
  terminal control other than tab, line feed, or carriage return fails load
  rather than being replaced or re-encoded.
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

When a backend launches either command, Avibe supplies internal bindings:

- `AVIBE_SKILL_WORKING_DIR` is the absolute working directory from which Avibe
  rendered that Session's Catalog. Project discovery always starts there, even
  when the agent runs the command from another directory.
- `AVIBE_SKILL_PROJECT_BASE`, when present, is the normalized absolute Avibe
  project base that bounded that Session's Catalog. It lets a command launched
  by an existing Session discover project Skills between a descendant working
  directory and a configured non-Git project base. Values outside the bound
  working directory's ancestor chain are ignored.
- `AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID` selects the version-scoped built-in snapshot
  retained by the Avibe process that created that backend runtime, even if an
  upgrade has since switched the stable `vibe` launcher to another artifact.
- `AVIBE_BUILTIN_SKILLS_ROOT` is that snapshot's absolute directory. It binds
  the complete runtime address, so a later launcher or `AVIBE_HOME` change
  cannot combine the retained snapshot ID with a different Avibe home.
- `AVIBE_SKILL_HOME`, `AVIBE_SKILL_CODEX_HOME`,
  `AVIBE_SKILL_CLAUDE_HOME`, and `AVIBE_SKILL_XDG_CONFIG_HOME` are normalized
  absolute roots resolved by the advertising Avibe process.
  `AVIBE_SKILL_CLAUDE_CLI_PATH` carries its configured Claude executable for
  plugin enumeration and load-time re-resolution. A relative
  `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, or `XDG_CONFIG_HOME` is therefore resolved
  once at Turn dispatch and cannot select a different compatibility tree merely
  because the agent launches `vibe skill` from another shell directory.

These values use the existing per-Session shell-environment path for each
backend: Claude SDK process environment, Codex `shell_environment_policy`, and
the OpenCode caller-context plugin. They are not prompt text or agent-visible
command arguments. A standalone command without Avibe bindings uses its own
current working directory and the snapshot bundled with its own executable.

An OpenCode binding normally lives for the active Avibe-dispatched Turn. Avibe
renews its released 24-hour expiry throughout active original and restored poll
loops; after a crash or an ambiguously abandoned poll, the last expiry remains
the bounded cleanup path. Binding-file updates are serialized across processes,
use a unique same-directory temporary file and atomic replacement, and cleanup
is guarded by a per-Turn token so an older Turn cannot refresh or remove a newer
binding for the same native Session. Each new or restored Turn makes its initial
publication before entering the active poll loop. A delayed initial publication
or renewal lost to a newer token stops instead of replacing that newer binding.
If the initial publication for a new Turn raises, Avibe logs the auxiliary
failure, starts its binding maintainer in the unbound state, and still sends the
prompt to OpenCode. The maintainer retries conditionally for the active Turn's
lifetime; binding-store availability never becomes a prerequisite for backend
execution.
The persisted shape remains readable by
an already-running OpenCode server that loaded the released plugin: the
existing `env`, `updated_at`, and `expires_at` fields are unchanged, while the
cleanup token and Skill roots are additive. This feature leaves the released
plugin source unchanged, so an upgrade can adopt its active server and restore
polls without requesting a plugin refresh. The adopting process reads the
binding-file path recorded for that managed server and continues to bind,
renew, and unbind there until the server is replaced, even when the effective
`AVIBE_HOME` has changed. A newly started server uses the current runtime path.
Expired entries are ignored and pruned.

The active-poll record retains the exact built-in snapshot ID and root that the
Turn's Catalog advertised. A process adopting that poll restores those values,
not the newer process default, so an overlapping upgrade cannot change a
same-Turn load.

Binding publication and cleanup run outside the controller event loop. A
restored poll makes three immediate publication attempts. If the binding store
remains unavailable, Avibe restores result delivery without waiting, then keeps
retrying conditionally for the lifetime of that active poll and resumes expiry
renewal after publication succeeds. An atomic ownership check at the binding
store permits the write only while no newer Turn owns the native Session. This
preserves the durable result path without permanently dropping the Turn's shell
binding after a transient failure or letting recovery overwrite current state.

### 8.3 Runtime access boundary

Workbench resource policies continue to authorize the Skills management API:
who may install, remove, or inspect packages through the Web surface. They do
not filter the runtime Catalog or `vibe skill load`. Once a Session can operate
an agent on the local machine, that agent can use ordinary filesystem tools to
read known Skill paths, so an environment-backed runtime filter would create a
false security boundary that the same shell could bypass.

The runtime Catalog is therefore the same resolved local capability set for
Claude, Codex, and OpenCode. Backend caller bindings select the advertised
working directory, compatibility roots, and built-in snapshot only. A real
per-user confidentiality boundary would require a sandboxed command and
filesystem broker and is outside v1.

### 8.4 Historical context is historical

Avibe does not track which Skills a Session has loaded, attach revisions to
loaded content, rewrite old conversation history, invalidate prior loads, or
require a Skill to be reloaded on every Turn.

Once a Skill body has been loaded, it remains historical context just like any
other file previously read by the agent. The current Catalog describes current
availability; it does not retroactively change history.

### 8.5 No watcher in v1

There is no filesystem watcher, change notification, incremental system-prompt
patch, or long-lived per-Session Catalog. Turn-time rendering is already the
correct consistency boundary, so a second refresh mechanism would add state
without improving the user-visible contract.

The initial implementation scans the fixed roots within the candidate and byte
budgets and reads only enough of each `SKILL.md` to extract frontmatter. A
reference measurement of six roots, 20 Skill files, and 242 KB total input
completed in about 1 ms median on a warm local filesystem. This is not a
latency guarantee; it shows that correctness can precede caching. Per-Turn
discovery runs outside the controller event loop so bounded cold-filesystem
latency cannot stall unrelated dispatch.

The common path performs no subprocess lookup when Claude has no installed
plugin registry. When that registry exists, the official plugin-list lookup is
also live per Turn and has the one-second failure boundary in Section 4.4.

V1 does not cache discovery results. A future parsed-frontmatter cache cannot
treat file identity, size, or timestamps as proof that content is unchanged: it
must read and digest the bounded frontmatter on that Turn before reusing a
parsed value. Any optimization must preserve the same freshness contract.

## 9. Backend Isolation and Prompt Application

Avibe disables native Skill presentation in the call path it controls, then
applies the same Avibe Catalog to all three backends.

| Backend | Native Skill isolation | Applying a changed Avibe prompt |
| --- | --- | --- |
| Claude Code | Configure the SDK with `skills=[]`. | Build the candidate prompt and complete Skill-binding state every Turn. If either changed, recreate the SDK client and resume the same native session; otherwise reuse the client. |
| Codex | Set `skills.include_instructions=false`. | Build `developerInstructions` every Turn. Send updated instructions only when they differ, while retaining the same thread/session. |
| OpenCode | Send `tools.skill=false` in every prompt request without rewriting user permission configuration. | Build and send the current system prompt on every new Turn. |

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
UTF-8 bytes of its `/`-separated relative path. A record contains the ASCII byte
`d` (`0x64`) for a directory or `f` (`0x66`) for a file, the path length as an
unsigned 64-bit big-endian integer,
and the path bytes. A file record additionally contains its byte length in the
same integer encoding, its exact bytes, and one byte holding
`st_mode & 0o111` where POSIX executable bits exist (zero otherwise). Release
packaging requires relative paths to be valid UTF-8 in NFC form and every
built-in `SKILL.md` body to be valid UTF-8. A fixed
tree-to-digest fixture freezes this encoding without creating a persisted
manifest. The identifier selects a directory; it is not a runtime integrity
protocol.

Built-in source paths must be representable without aliases on every supported
platform. Release packaging rejects absolute or traversal paths, backslashes,
NUL and every Win32-forbidden control character (`U+0001`-`U+001F`), plus
every forbidden component character (`<`, `>`, `:`, `"`, `\`, `|`, `?`,
`*`), drive/UNC prefixes,
Windows-reserved components, trailing-dot/space names, case-insensitive path
collisions, and empty directories. It also rejects more than 1,024 built-in
root entries, any direct child that is not a valid Skill directory, duplicate
declared Skill names, more than 4,096 directories and regular files across the
complete tree, more than 32 MiB of regular-file bytes across the complete tree,
any Skill whose closing frontmatter delimiter exceeds 64 KiB, any body over 256
KiB, any invalid UTF-8 `SKILL.md` body, or a built-in tree whose bounded
frontmatter exceeds 8 MiB. The aggregate
entry traversal is capped while enumerating. Hashing and publication also charge
the size of each regular file at the point it is opened and bound every read to
that size, so growth after enumeration cannot exceed the aggregate byte budget.

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
The next lock holder first removes the one deterministic hidden staging path
for that digest, reclaiming any partial tree left by an interrupted publisher.
It then re-enumerates one bounded, fixed entry set and copies only those entries
into staging; entries created after that enumeration are neither traversed nor
copied. Each file is reopened without following its final path component,
charged against the aggregate byte budget, read to its opened size, and rejected
if it changes during the read. Source and destination descriptors use binary
mode where the platform distinguishes it, so publication preserves exact bytes
on Windows as well as POSIX systems. Avibe then validates that staging
directory, recomputes the canonical snapshot-v1 digest from the completed
staging tree, and requires it to equal the target `<snapshot-id>` before
atomically renaming it once into the previously absent digest path. The staged
digest includes every
file byte and executable mode, so copied bytes cannot diverge from the name
under which they are published. A process interruption can leave only that
undiscoverable, bounded staging directory, and the next attempt safely replaces
it before retrying. If the digest path already exists,
concurrent or later publishers reuse that path without
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

`$PROJECT_BASE` is the Git project root when discovery finds it within the
128-directory ascent bound and the active working directory otherwise. The
installer and resolver share this bounded project-base rule, so an installed
project Skill is always in a root that the next Turn scans.

The Workbench exposes one installation, not a backend selector or per-backend
toggle. New installs invoke askill for Claude Code, Codex, and OpenCode
together. askill keeps the authoritative directory in the backend-neutral
`.agents/skills` target and may create its normal backend-native compatibility
links; native presentation is disabled in Avibe's runtime path, so those links
do not create three product-level copies.

Existing backend-native installs remain in place and are discovered as
compatibility inputs; Avibe does not move or rewrite user files during this
migration. New backend-neutral installs create all three legacy Workbench
access-policy identifiers for the one logical `.agents` Skill, and logical
removal removes all three identifiers and askill-managed links. Existing
backend-native Skills retain their existing management-policy identities.
These identifiers govern the management surface only and do not narrow the
runtime Catalog. The API may continue accepting a
legacy `backends` field for compatibility, but validates and ignores narrowing
requests. The UI removes backend filters, chips, install selectors, and
availability switches.

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
management surface. This document supersedes its backend selection and
per-backend availability model as well as assumptions about using native Skill
Catalogs at runtime.

## 14. Acceptance Criteria

### 14.1 Resolver and protocol

- A fixture covering every discovery root, including Codex's `.system`
  container, resolves one entry per final name.
- Enabled Claude plugins contribute their standard `skills` directories to the
  same Avibe Catalog for all three backends, while disabled plugins do not.
  Enabled plugin Skills override same-name Codex `.system` defaults but not
  same-name user Skills from the four static compatibility roots.
  A custom V2 Claude CLI path is used for both Turn-time and bound-command
  plugin enumeration.
  Missing, failing, timed-out, malformed, oversized, or over-count plugin-list
  results omit only plugin roots, and static compatibility discovery continues;
  the combined standard output and standard error limit is enforced during
  capture.
- Global-root fixtures override `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and
  `XDG_CONFIG_HOME` with relative and absolute values; Turn bindings normalize
  them to the same absolute homes used by their live backends, independent of
  the command's later shell directory.
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
- A filesystem-observable in-place rewrite, truncation, or replacement of
  `SKILL.md` during the verified read is rejected: discovery omits it and load
  exits non-zero with empty standard output. Atomic namespace replacement after
  the file is opened is covered separately from in-place handle mutation, and
  coarse filesystems retain the best-effort boundary stated in Section 4.1.
- Frontmatter parsing reads no more than 64 KiB before accepting or omitting a
  candidate, including for oversized or unterminated input.
- A root with more than 1,024 direct children is omitted after enumerating at
  most 1,025 entries. Built-ins and combined project/global compatibility
  inputs each have separate 4,096-direct-child, 1,024-candidate, and 8 MiB
  frontmatter budgets, so either class can exhaust its own budget without
  consuming the other's. Cross-root exhaustion follows precedence and the
  pre-frontmatter path order defined in Section 5.1. The Codex `.system`
  container counts toward its parent's raw 1,024-child limit regardless of
  enumeration order, while its contents are scanned only as the explicit
  system root.
- Compatibility directory symlinks resolve to their target directory, and
  replacing either the alias or target after discovery makes load fail. A
  canonical Skill plus all backend compatibility aliases consumes one candidate
  and one frontmatter slot while every enumerated alias still consumes one
  direct-child slot. A candidate whose resolved absolute directory cannot be
  encoded as UTF-8 is omitted before Catalog or load output.
- Unquoted YAML comments after `name` or `description` are ignored while `#`
  inside a quoted scalar remains content.
- Comment-only and trailing-comment lines around indented plain continuations
  of required scalars are ignored by the tolerant fallback parser.
- Standard escapes in a quoted name are decoded before portable-name
  validation, and indented continuation lines in a plain description remain
  part of its normalized Catalog value.
- Valid quoted `name` and `description` mapping keys, including standard escapes
  in those keys, are accepted. The bounded node parser never constructs typed
  optional values, while the tolerant scanner still extracts managed fields
  when unrelated metadata is malformed, deeply nested, or recursively aliased.
- A Skill declaring `disable-model-invocation: true` is absent from every
  Catalog page but remains loadable by an explicitly supplied portable name.
  When every resolved Skill has that policy, the prompt retains generic
  exact-name load guidance without exposing any protected name or description;
  a truly empty resolver still emits no Skill block.
- Prompt and `vibe skill list` pagination are deterministic for an unchanged
  filesystem, limited to 25 entries, remain within the row budget, and do not
  expose paths or sources. A multibyte-description fixture proves that every
  later page starts after the last row actually emitted, without skipping rows
  when the byte budget wins before the count limit. When later pages exist, the
  prompt makes further discovery optional, while a user-requested exact name
  may load directly. A fixture mutating the Catalog between page calls verifies
  live re-resolution and the documented restart-at-page-1 boundary rather than
  a hidden snapshot.
- Oversized descriptions cannot make the Catalog unbounded, and names with
  whitespace, shell syntax, uppercase characters, or invalid hyphen placement
  are omitted by the parser-backed portable name boundary. YAML-decoded C0 and
  C1 control characters cannot reach terminal Catalog output.
- `vibe skill load` emits the exact XML wrapper, body only, and an absolute
  directory from which supporting files can be read. A control-character path
  fixture proves the wrapper contains only the reversible ASCII references and
  no raw terminal controls.
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
- An invalid UTF-8 body may be advertised from its valid frontmatter but load
  produces empty standard output and a non-zero exit without replacement text.
- A body containing C0/C1 terminal controls other than tab, line feed, or
  carriage return may be advertised from its valid frontmatter but load fails
  with empty standard output instead of emitting those bytes.
- Built-in publication rejects an invalid UTF-8 body, so every published
  built-in that can be advertised is also loadable under the body-encoding
  contract.
- A Workbench Session working below its configured non-Git project directory
  discovers project-level Skills up to that directory on the next Turn and via
  `vibe skill`; an invalid, non-ancestor, or over-depth project binding is
  ignored. Persisted pre-upgrade fixtures cover both deriving an unchanged
  scope's ancestor and retaining that ancestor across a later scope move.
- Workbench resource policies continue to govern management operations but do
  not narrow the runtime Catalog or loaded body, matching the ordinary
  filesystem capability of the agent process.

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
- OpenCode user permission configuration and released caller-context plugin
  source are not rewritten, concurrent active
  Turn bindings preserve each other, and token-guarded cleanup removes only the
  binding created by that Turn; active original and restored polls renew their
  released-shape expiry, an abandoned binding expires without a live renewer,
  and each restored Turn replaces its own entry without pruning other unexpired
  Sessions. When a managed server is adopted across an `AVIBE_HOME` change,
  bind, renew, and unbind operations continue using the absolute binding path
  recorded by that server. Restored polls retain their advertised built-in
  snapshot and continue conditionally retrying a failed binding publication for
  the poll lifetime; a new Turn likewise reaches OpenCode after an initial
  binding exception and retries for its active lifetime; a newer Turn token
  makes the delayed retry stop without replacing current state; and
- a cached Claude client is recreated and resumes the same native Session when
  its bound Skill roots or built-in snapshot change even if page 1 is identical;
  and
- each adapter retains the same native Session when the Catalog changes.

Codex `$skill`, backend TUI commands, and ordinary filesystem reads are not v1
isolation acceptance gates.

### 14.4 Built-in lifecycle

- a fresh installation mirrors all bundled Skills;
- a real wheel-install fixture proves bundled Skills exist without a source
  tree and preserves executable modes required by helper scripts;
- a real sdist-install fixture builds from the source archive, then proves the
  resulting installation publishes and loads the same bundled Skills without
  a repository checkout;
- a fixed snapshot-v1 fixture proves the canonical tree byte stream and
  lowercase SHA-256 identifier remain stable;
- release packaging accepts only directories and regular files throughout the
  complete built-in tree, and every relative path has one unambiguous,
  cross-platform representation. Representative fixtures cover links and
  special files, Win32-forbidden or aliased components, empty directories, and
  every declared entry, byte, frontmatter, body, and root-child bound;
- release packaging rejects duplicate declared Skill names even when their
  directories differ;
- a mode-only built-in change produces a different snapshot and published
  digest;
- publication recomputes the snapshot-v1 digest from the completed staging
  tree and refuses to rename staging bytes under a different digest;
- publication opens destination files in binary mode where available and
  preserves mixed newline bytes exactly;
- publication ignores entries created after its bounded copy enumeration and
  rejects aggregate file growth observed at hashing or copy time;
- an upgrade's selected snapshot contains changed Skills and omits retired
  Skills;
- after an injected interruption leaves partial staging state, the next
  publisher reclaims it and completes successfully without exposing a partial
  snapshot;
- a wrong-type or unreadable pre-existing digest path fails safely without
  being traversed, mutated, or rebuilt by runtime commands;
- two concurrently running artifacts with different bundled trees resolve
  different version-scoped snapshots;
- after launcher activation, a command inherited from the older runtime still
  lists and loads that runtime's retained built-in snapshot path without the
  older package source and never falls back to the newer snapshot, even when
  the active `AVIBE_HOME` changed; and
- every loaded built-in reports an agent-accessible absolute directory.

## 15. Explicit Non-Goals

V1 does not include:

- unified global or project prompt files;
- disabling native `AGENTS.md` or `CLAUDE.md` loading;
- a new Skill compatibility schema;
- source namespaces or source disclosure to the agent;
- Avibe-specific semantic ranking or compatibility filtering beyond preserving
  an existing manual-only invocation declaration;
- Skill editing or copy-on-write;
- semantics for `${AVIBE_HOME:-$HOME/.avibe}/skills`;
- garbage collection of old built-in snapshots;
- post-publication tamper detection or self-healing for Avibe-owned built-in
  snapshots;
- live filesystem watchers or historical-context invalidation; or
- a security sandbox preventing direct filesystem access.
