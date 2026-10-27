# Avibe MCP Hub MVP design

> Status: implemented (instance-global Hub). Official latest stable Python MCP SDK, no OAuth, no generated Skills.
> Model: discover tools at generation time, persist a static CLI package, explicitly regenerate to update.
> Principle: smallest complete design; reuse existing caller context, IPC, credentials, i18n and owner lifecycle; no speculative defenses.

## 1. Change contract

Add MCP Hub to the Skills page. A user pastes a provider `mcpServers` JSON block (or runs `vibe mcp add`), saves it, and generates a package. Every backend calls tools through `vibe mcp <server> <tool>`; no Skill loader.

**Native MCP is off; Hub owns the same files.** Claude, Codex and OpenCode must not load their own MCP servers in Avibe sessions. Hub does not copy configs into `~/.avibe/mcp` as a second store of connection definitions. It reads and writes the backends' existing files:

- Claude: `~/.claude.json` `mcpServers`
- Codex: `~/.codex/config.toml` `[mcp_servers.<name>]`
- OpenCode: `~/.config/opencode/opencode.json` `mcp`

Avibe-only runtime artifacts (generated CLI revision, enabled flag, current pointer) still live under `<avibe-home>/mcp/<name>/`. Connection fields (command, url, env, headers) are edited in the native file. Disable native loading with `strict_mcp_config=True` + empty `mcp_servers` for Claude, an overlay Codex config without `[mcp_servers]`, and OpenCode `mcp.*.enabled=false` / empty overlay at spawn — never delete the user's files.

```text
paste / vibe mcp add → save definition → generate (connect, SDK protocol negotiation, tools/list all pages) → publish static revision
```

In scope: stdio and Streamable HTTP, tool discovery and calling, plaintext env/header configuration, generate/regenerate, static help/list, shared runtime, structured/multimodal results.

Out of scope: OAuth, SSE (explicit `unsupported_transport_sse` error), project scope, Skill generation, resources/prompts, sampling, update detection, auto-install/upgrade of server software, package sharing/import.

## 2. Package on disk

```text
<avibe-home>/mcp/<name>/
  state.json               # Avibe-side: enabled, description, last native source
  current.json             # pointer to the published revision
  revisions/<revision>/    # <revision> = generation timestamp + random suffix
    definition.json        # immutable snapshot of the definition used to generate and to connect
    manifest.json          # formatVersion, description, generatedAt, serverInfo.version if the server provides it (optional; shown in UI)
    tools.json             # original tool descriptors
    commands.json          # generated flags and help
```

- Native files are the connection store. Avibe home holds `state.json`, `current.json` and revision snapshots (including `revisions/*/definition.json`). Nothing is duplicated into V2 config. Every file is written with the existing `config/atomic_io.py` `write_atomic`.
- `commands.json` is declarative data interpreted by the shared `vibe mcp` runner, not code. Tool implementations stay on the server/executable.
- Revisions are derived data: a revision whose `formatVersion` this release does not read is shown as "needs regeneration" (definition kept, nothing deleted) instead of being migrated. Any other unreadable, invalid or future-format file of a package (`definition.json`, `state.json`, `current.json`) disables only that package with a warning; startup never fails and bad files are not overwritten.

## 3. Prompt injection

The shared system prompt gets a short discovery index (outside `core/managed_skills.py`), only when at least one package is enabled and generated:

```text
## MCP capabilities

Use `vibe mcp <server> --list` to discover available tools.
Use `vibe mcp <server> <tool> --help` to inspect arguments before calling.
Call tools with `vibe mcp <server> <tool> [arguments]`.
Help and lists read local generated definitions; tool calls access the MCP server.

Available servers:
- context7: Find current library documentation and code examples.
- github: Work with GitHub repositories, issues, and pull requests.

List all servers: `vibe mcp --list`.
```

- One line per package, sorted by name; description sanitized to one line, max 160 chars.
- Description: from the active revision's manifest (user-provided at generation, else a localized generic string); pending unregenerated edits are not shown. Server-provided text is not used.
- List at most 50 servers; if more exist, append the omitted count and `vibe mcp --list`.
- No tool names, schemas, URLs, commands, credentials or management commands.
- The block has two fixed parts: stable shared instructions (examples must stay parser-valid), then one `<server>: <description>` line per package, where `<server>` is the exact CLI selector.
- Disable/remove takes effect on the next prompt build; new calls are rejected immediately even if a running turn still sees the old list.
- The index grants nothing: every call rechecks enabled state at the owner.

Agents discover details on demand:

```text
prompt index
  → vibe mcp context7 --list
  → vibe mcp context7 resolve-library-id --help
  → vibe mcp context7 resolve-library-id --args-json '{"libraryName":"react","query":"React hooks"}'
```

## 4. CLI

### 4.1 Discover and call

```bash
vibe mcp --list                         # enabled packages; --all includes disabled/draft/error
vibe mcp docs --list [--format json]
vibe mcp docs search --help
vibe mcp docs search --query 'async Python' --limit 5
vibe mcp docs search --args-json '{"query":"async Python","limit":5}'   # also --args-file, --args-stdin
```

- List/help read local artifacts only; they work offline and with the owner down.
- Lists print all entries, sorted by name. Human-readable output escapes control characters from server-provided text.
- Calls go to the owner, print one JSON document to stdout, diagnostics to stderr.
- Errors point to the next useful command (root list, service list or tool help).

Exit codes: `0` success, `2` usage/argument error, `1` any other failure (including command-admission denial). The JSON `error.type` carries the category (`not_found`, `disabled`, `connection`, `tool_error`, `timeout`, `capacity`, …). `unknown_outcome` marks any failure after `tools/call` was sent other than a returned tool error or a known pre-dispatch rejection; it must not be retried. An IPC timeout or disconnect after the CLI submitted the call to the owner is also `unknown_outcome`, following the existing IPC acceptance-ambiguity handling in `vibe/internal_client.py`.

### 4.2 Schema to flags

| Schema type | Flag |
|---|---|
| string | text as-is |
| number / integer | strict JSON number |
| boolean | explicit `true` / `false` |
| array / object | JSON value |
| union / nullable / complex, or key not matching `[A-Za-z][A-Za-z0-9_-]*` or named `args-json`/`args-file`/`args-stdin`/`help` | only via `--args-*` |

- Exact property keys; no aliases, case conversion or inserted defaults.
- One input mode per call; no input means `{}`.
- A tool whose name starts with `-` is selected after `--`: `vibe mcp docs -- --list --args-json '{}'`.
- Validate with a standard JSON Schema library (no remote refs). A tool whose schema it cannot handle is published as JSON-input-only with unvalidated help, not a failed generation.

### 4.3 Management

```bash
vibe mcp add --transport http docs https://example.invalid/mcp --header 'Accept-Language: en'
vibe mcp add playwright -- npx -y @playwright/mcp@latest
vibe mcp add local-tools --env MODE=readonly -- python server.py
vibe mcp add --json-stdin
vibe mcp generate|regenerate|enable|disable|remove <name>
```

- Syntax follows the [Claude Code `mcp add`](https://code.claude.com/docs/en/mcp) subset, including its `--env` placement rule (after the name, or separated from it by another option); stdio is default, argv after `--` is verbatim.
- Avibe extensions: `--description`, `--cwd`, `--replace`, `--json-stdin`.
- `--scope`, SSE and OAuth options fail explicitly.
- Add only saves. An existing name needs `--replace`, which keeps state and the active revision until regeneration.
- Names: whatever the native config or user provides. Reject only empty names, `.`/`..`, NUL, or `/`/`\\` (names become directory names). `add`/`generate`/`regenerate`/`enable`/`disable`/`remove` stay reserved because `vibe mcp <verb>` would otherwise be ambiguous. Immutable in MVP.

## 5. Page configuration

Flow: paste → **Parse and preview** → **Save** → **Generate** (later **Regenerate**). Parse/preview/save never run or connect to anything.

```json
{
  "mcpServers": {
    "docs": {"type": "http", "url": "https://example.invalid/mcp", "headers": {"Accept-Language": "en"}},
    "local-tools": {"command": "/absolute/path/to/server", "args": ["--read-only"]}
  }
}
```

- Accept the common provider shape: `command/args/env/cwd` with absent or `stdio` type → stdio; `url` with absent/`http`/`streamable-http`/`streamable_http` type → Streamable HTTP. Reject conflicting or unsupported fields with a field-specific message. Provider `enabled` is ignored.
- Env and header values are stored as given, in plaintext, like Claude Code's own MCP config. No Vault integration in MVP. `<avibe-home>/mcp/` is created with the existing owner-private helpers (`core/private_fs.py`: `ensure_private_directory`, `make_private_file`; POSIX modes, Windows DACL); UI/API mask env/header values using the existing config-secret pattern (`has_<field>` metadata; an empty submitted value means "unchanged", see `_strip_preserved_config_secrets` in `vibe/api.py`; env/header maps are submitted whole, so a key left out is deleted); Avibe's own logs never print them. A server's own stderr is the server's output (as in Claude Code) and is not scrubbed.
- URLs with embedded credentials (`user:pass@`) are rejected; put them in an `Authorization` header.
- `${VAR}` / `${VAR:-default}` placeholders are not expanded; parse rejects them with a field-specific message asking for the literal value.
- Hub-entered `cwd` (paste, `vibe mcp add`) must be absolute or omitted. A relative `cwd` read from any native file resolves against that file's directory (it always exists, so no HOME fallback is needed); `~` is expanded; only an empty or non-string `cwd` is rejected. The process starts there, so a relative `./command` resolves against it too. Codex does not document its own base (observed bases differ by version), so Hub uses one rule for every source. A natively `enabled=false` entry is omitted from Hub lists (CLI, API, UI, prompt index). Leftover generated artifacts for that name are not listed while the native off switch remains. Spawn isolation still disables those names. Provider `enabled` is still ignored on Hub save. Parse errors on a natively disabled entry are also omitted.
- If a generated name disappears from its native file, Hub keeps the package (revision snapshot, enabled state, calls). Lists/API/UI mark it `nativeDeleted` ("deleted in Claude/Codex/OpenCode"). Drafts with no revision vanish. Re-adding the name natively clears the flag; definition drift is pending regenerate. Explicit `vibe mcp remove` / UI delete is how artifacts go away.
- Existing names require explicit replace; omitted services are untouched.

Persisted `definition.json`:

```json
{
  "schemaVersion": 1,
  "name": "docs",
  "description": "Search current library documentation",
  "transport": {
    "type": "streamable_http",
    "url": "https://example.invalid/mcp",
    "headers": {"Authorization": "Bearer <token>"}
  }
}
```

## 6. Generation and versions

- Uses its own connection; the SDK's high-level client negotiates the protocol; then fetches all `tools/list` pages (cursors are opaque; stop when absent; fail after 100 pages), builds the revision.
- Publish = write the full revision directory, then atomically replace `current.json`. Any failure leaves the previous revision active.
- The owner serializes save/generate/enable/disable/remove per package, so these never race.
- Initial generate publishes, then sets `state.json` enabled; a crash in between leaves a generated but disabled package that the user enables. Regenerate never changes `state.json`.
- Keep the current and previous revision; delete older ones after publish. All readers go through one read helper that, if a revision file is missing, rereads `current.json` once.
- Every generate publishes a new revision; there is no content hashing or "no change" detection. Nothing compares versions or detects updates; `tools/list_changed` is ignored.
- Regenerate does not upgrade server software. A stale schema surfaces as a call error suggesting regeneration; calls are never replayed.
- Calls connect with the admitted revision's `definition.json` snapshot (owner-private, may hold plaintext values), never the edited top-level file, so a pending edit cannot mix old schemas with a new URL/header/argv. Edits take effect on regeneration.
- Tools with `execution.taskSupport: "required"` are skipped with a generation diagnostic (tasks are out of scope).

## 7. Runtime

- **Owner:** the Controller, over existing control IPC. UI and CLI hold no runtimes.
- **Identity:** existing caller context (`core/caller_context.py`, `AVIBE_SESSION_ID`, …) as used by `vibe task/watch/agent`. No MCP-specific token.
- **Permission:** instance-global. The local OS user has full rights. Management commands (add/save, generate/regenerate, enable/disable, remove) are Owner-only through the existing CLI command admission, because generating a stdio definition runs an arbitrary local command; list/help (admission runs before any help is rendered) and tool calls use the same floor as using Skills (Editor), matching who sees the prompt index. Remote-context callers thus get exactly the permissions they have elsewhere. No MCP-specific policy.
- **Connections:** one client connection per (revision, caller session) for every transport — a stdio process, or an HTTP client holding its own `Mcp-Session-Id` (closed with the connection). This matches Claude Code and Codex (one client per session). Context-free calls are one-shot. Calls within one session are serialized on its connection. A connection whose call ends with `unknown_outcome` is closed, never reused. An HTTP `Session not found` (pre-dispatch) closes the client and the call is retried once on a fresh connection.
- **Limits** (constants): at most 8 stdio processes in total, including generation; at the cap a new connection fails with `capacity`. Idle close 10 min; call timeout 120 s; generation 5 min. HTTP is not capped.
- Servers that lock a shared resource (e.g. Playwright's default persistent profile) need their own isolation flag in the provider config (`--isolated`); Avibe does not rewrite server argv.
- **Schemas at call time:** the CLI parses and validates against the published revision only. A new runtime connection lists all `tools/list` pages through the SDK before its first call (same 100-page cap as generation), so the SDK knows every tool's output schema and header annotations; that list never changes the published package.
- **Calls:** the owner checks enabled state, connects or reuses, sends `tools/call` and returns the result with the revision used. Elicitation/sampling/roots are out of scope: the client's callbacks for them reject; since the tool may already have acted, the call ends as `unknown_outcome`. The owner fixes the revision at admission and validates arguments against it.
- **Disable/remove/regenerate:** disable/remove block new calls; all three close the package's superseded idle connections at once (busy ones close when their call settles); in-flight work may finish or report unknown outcome.
- **Event loop:** all MCP SDK sessions run on one dedicated runtime thread with its own event loop, owned by the Controller; the SDK's synchronous result validation therefore never blocks the Controller loop. All other MCP work — argument validation, generation, artifact I/O, result envelope serialization and sizing — also runs off the Controller loop; the Controller loop only hands IPC requests over, per the repo's controller-loop rules.

Result envelope:

```json
{"schemaVersion": 1, "server": "docs", "revision": "<hex>", "tool": "search", "status": "ok",
 "result": {"content": [], "isError": false}}
```

Preserve typed content, `structuredContent`, `isError` and explicit null/false/empty values. Results above a fixed byte cap (set in C0; no shared IPC cap exists) are not returned: the call still completed, so the envelope keeps `isError` and reports `resultOmitted: "too_large"` — success, or the normal tool error when `isError` is true — so Agents never retry a committed call.

## 8. UI / API

UI shows each package's state (draft / generated / error), enabled toggle, active revision, server version and pending definition changes. Actions: paste/preview/save, generate or regenerate (one button), enable/disable, delete, browse static tools. All copy through i18n.

```text
GET    /api/mcp
POST   /api/mcp/parse
POST   /api/mcp/save
GET    /api/mcp/{name}
PATCH  /api/mcp/{name}
POST   /api/mcp/{name}/generate     # initial or regenerate; same owner operation as the CLI
GET    /api/mcp/{name}/tools        # published artifact
POST   /api/mcp/{name}/enable
POST   /api/mcp/{name}/disable
DELETE /api/mcp/{name}
```

Native async FastAPI routes calling the owner over IPC. Routes are classified in the existing Web route authorization (`vibe/authorization.py`): read routes (`GET /api/mcp`, `GET /api/mcp/{name}`, `GET /api/mcp/{name}/tools`) at Editor like Skills, management routes Owner-only; and forward the validated Web user context to the owner like existing config APIs do.

## 9. Prior art

Lifecycle follows mcporter `generate-cli` (inspected at [`5362d60`](https://github.com/openclaw/mcporter/blob/5362d60d2e17ca5b2dee3d92f48c0fb7b010abc5/docs/cli-generator.md)): discover at generation, static help, explicit regeneration. Differences: one shared Python runner instead of per-server executables; exact keys with no flag normalization or default insertion.

## 10. Implementation and acceptance

- **C0** (after authorization): pin SDK, confirm caller-context/IPC fit, SDK protocol negotiation, and the result byte cap. Hermetic temp `AVIBE_HOME` and loopback fixtures.
- **C1:** repository + parser, generator/publisher, runtime, CLI, prompt index.
- **C2:** UI/API, i18n, user documentation (adding servers, plaintext credential behavior, regeneration, tool invocation), Ruff and UI build; Incus regression on request.
- **C3:** Disable Claude/Codex/OpenCode native MCP in Avibe sessions. Hub reads/writes those backends' config files as the connection store (no copy-import). Generated CLI artifacts remain under `<avibe-home>/mcp/`.

Acceptance:

1. Page and `vibe mcp add` produce identical definitions; Claude-style syntax is parser-tested; add never connects; env/header values live only in owner-private definition files, and are masked in UI/API and never printed by Avibe's own logs.
2. Help/list work with the server and owner down; schema-to-flag contracts hold; injected prompt examples are parser-tested.
3. Generation never publishes partial tool lists; readers never see mixed revisions; initial generate enables, regenerate preserves state.
4. Tool execution uses `tools/call` against the admitted revision (SDK-internal auxiliary requests allowed); unknown-outcome connections are closed; process cap, idle close and timeouts hold; unknown outcomes are not retried.
5. Results keep structured/multimodal/error content; disabled packages reject calls; tests cannot touch real user state.
6. Avibe-spawned Claude/Codex/OpenCode sessions do not load native MCP servers; Hub add/save updates the corresponding native config; `vibe mcp generate` still publishes Avibe artifacts.

## 11. Next action

Owner review, then C0 on explicit authorization.
