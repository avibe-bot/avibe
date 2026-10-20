# Avibe MCP Hub MVP design

> Status: design only. No implementation, dependency installation, commit or push is authorized by this document.
> Decided: official latest stable Python MCP SDK (pin at implementation), instance-global scope, no OAuth, no generated Skills.
> Model: discover tools at generation time, persist a static CLI package, explicitly regenerate to update.
> Principle: smallest complete design; reuse existing caller context, IPC, credentials, i18n and owner lifecycle; no speculative defenses.

## 1. Change contract

Add MCP Hub to the Skills page. A user pastes a provider `mcpServers` JSON block (or runs `vibe mcp add`), saves it, and generates a package. Every backend calls tools through `vibe mcp <server> <tool>`; no backend-native MCP config, no Skill loader.

```text
paste / vibe mcp add → save definition → generate (connect, initialize, tools/list all pages) → publish static revision
```

In scope: stdio and Streamable HTTP, tool discovery and calling, plaintext env/header configuration, generate/regenerate, static help/list, shared runtime, structured/multimodal results.

Out of scope: OAuth, SSE (explicit `unsupported_transport_sse` error), project scope, Skill generation, resources/prompts, sampling, update detection, auto-install/upgrade of server software, package sharing/import.

## 2. Package on disk

```text
<avibe-home>/mcp/<name>/
  definition.json          # editable connection definition (may hold plaintext env/header values)
  state.json               # {"enabled": bool}
  current.json             # pointer to the published revision
  revisions/<revision>/
    manifest.json          # versions, effective definition, description
    tools.json             # original tool descriptors
    commands.json          # generated flags and help
```

- `definition.json` and `state.json` are the only authorities; nothing is duplicated into V2 config or another registry.
- `commands.json` is declarative data interpreted by the shared `vibe mcp` runner, not code. Tool implementations stay on the server/executable.
- Invalid or future-format files disable that package with a warning; startup never fails and bad files are not overwritten.

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
- Description: user-provided, else a localized generic string. Server-provided text is not used.
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
vibe mcp docs --list [--page N] [--page-size N] [--format json]
vibe mcp docs search --help
vibe mcp docs search --query 'async Python' --limit 5
vibe mcp docs search --args-json '{"query":"async Python","limit":5}'   # also --args-file, --args-stdin
```

- List/help read local artifacts only; they work offline and with the owner down.
- Pagination: default 20, max 100, sorted by name; JSON output includes `hasMore`.
- Calls go to the owner, print one JSON document to stdout, diagnostics to stderr.
- Errors point to the next useful command (root list, service list or tool help).

Exit codes (the JSON error `type` is authoritative; command-admission denials keep the existing `vibe` CLI exit code 1):

| Code | Meaning |
|---:|---|
| 0 | success |
| 2 | syntax / argument validation |
| 3 | package or artifact missing/invalid, or `package_changed` |
| 4 | package disabled |
| 5 | connection / unsupported transport |
| 6 | tool not found or tool error |
| 7 | timeout or cancelled before `tools/call` was sent |
| 8 | timeout, cancel or disconnect after `tools/call` was sent (unknown outcome) |
| 9 | owner unavailable or at capacity |

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
- Names: `[a-z][a-z0-9-]{0,62}`, excluding `add`, `generate`, `regenerate`, `enable`, `disable`, `remove` and Windows reserved device names (`con`, `prn`, `aux`, `nul`, `com1`–`com9`, `lpt1`–`lpt9`), since names are directory names on every platform. Immutable in MVP.

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

- Accept the common provider shape: `command/args/env/cwd` → stdio; `url` with absent/`http`/`streamable_http` type → Streamable HTTP. Reject conflicting or unsupported fields with a field-specific message. Provider `enabled` is ignored.
- Env and header values are stored as given, in plaintext, like Claude Code's own MCP config. No Vault integration in MVP. `<avibe-home>/mcp/` is created with the existing owner-private helpers (`core/private_fs.py`: `ensure_private_directory`, `make_private_file`; POSIX modes, Windows DACL); UI/API mask env/header values using the existing config-secret pattern (`has_<field>` metadata; an empty submitted value means "unchanged", see `_strip_preserved_config_secrets` in `vibe/api.py`); Avibe's own logs never print them. A server's own stderr is the server's output (as in Claude Code) and is not scrubbed.
- `${VAR}` / `${VAR:-default}` placeholders are not expanded; parse rejects them with a field-specific message asking for the literal value.
- `cwd` must be absolute; absent means the sanitized Agent default.
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

- Uses its own connection; the SDK's high-level client negotiates the protocol (modern discovery or legacy `initialize`) and the selected version is recorded; then fetches all `tools/list` pages (stop on absent cursor; fail on repeated cursor or >100 pages), builds the revision.
- Publish = write the full revision directory, then atomically replace `current.json`. Any failure leaves the previous revision active.
- The owner serializes save/generate/enable/disable/remove per package, so these never race.
- Initial generate enables the package on success: it writes `state.json` (enabled) before switching `current.json`, so a crash in between leaves an enabled draft that simply regenerates; no transaction needed. Regenerate never changes `state.json`.
- Keep the current and previous revision; delete older ones after publish. An offline list/help reader that finds a revision file missing rereads `current.json` once and retries.
- `revision` = sha256 of the canonical manifest inputs (definition, versions, tools, commands), excluding timestamps. An identical rebuild reports "no change".
- Manifest records `protocolVersion`, `serverInfo`, `generatorVersion`, `generatedAt` as provenance only; nothing compares them or detects updates. `tools/list_changed` is ignored.
- Regenerate does not upgrade server software. A stale schema surfaces as a call error suggesting regeneration; calls are never replayed.
- The manifest embeds the complete effective definition (including env/header values; owner-private via the same helpers, masked in UI). Calls always connect with the admitted revision's own definition, never the edited `definition.json`, so a pending edit cannot mix an old URL with a new header. Changing a value is a definition change and needs regeneration.
- Tools with `execution.taskSupport: "required"` are skipped with a generation diagnostic (tasks are out of scope).

## 7. Runtime

- **Owner:** the Controller, over existing control IPC. UI and CLI hold no runtimes.
- **Identity:** existing caller context (`core/caller_context.py`, `AVIBE_SESSION_ID`, …) as used by `vibe task/watch/agent`. No MCP-specific token.
- **Permission:** instance-global. The local OS user has full rights. Management commands go through the same existing CLI command admission (role floor) as other config-changing `vibe` commands, so remote-context callers get exactly the permissions they have elsewhere. No MCP-specific policy.
- **Connections:** one client connection per (revision, caller session) for every transport — a stdio process, or an HTTP client holding its own `Mcp-Session-Id` (closed with the connection). This matches Claude Code and Codex (one client per session). Context-free calls are one-shot. Calls within one session are serialized on its connection.
- **Limits** (constants): at most 8 stdio processes in total, including generation. At the cap, close the longest-idle process (no in-flight call) to make room; fail with exit 9 only if all are busy. Idle close 10 min; call timeout 120 s; generation 5 min. HTTP is not capped.
- Servers that lock a shared resource (e.g. Playwright's default persistent profile) need their own isolation flag in the provider config (`--isolated`); Avibe does not rewrite server argv.
- **Schemas at call time:** the CLI parses and validates against the published revision only. The runtime SDK client may run its own `tools/list` when the protocol needs it (e.g. `x-mcp-header` → `Mcp-Param-*` headers); that never changes the published package.
- **Calls:** the owner checks enabled state, connects or reuses, sends `tools/call` and returns the result with the revision used. If `current.json` changed between CLI parse and admission, it returns `package_changed`.
- **Disable/remove:** blocks new calls and closes connections; in-flight work may finish or report unknown outcome.
- **Event loop:** all MCP SDK sessions run on one dedicated runtime thread with its own event loop, owned by the Controller; the SDK's synchronous result validation therefore never blocks the Controller loop.

Result envelope:

```json
{"schemaVersion": 1, "server": "docs", "revision": "<hex>", "tool": "search", "status": "ok",
 "result": {"content": [], "isError": false}}
```

Preserve typed content, `structuredContent`, `isError` and explicit null/false/empty values. Results above a fixed byte cap (set in C0; no shared IPC cap exists) are not returned: the call still completed, so the envelope reports `status: "ok"` with `resultOmitted: "too_large"` and exit 0, so Agents never retry a committed call.

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

Native async FastAPI routes calling the owner over IPC. Management routes use the existing Web route authorization (`vibe/authorization.py`) at the same role as other config-changing routes, and forward the validated Web user context to the owner like existing config APIs do.

## 9. Prior art

Lifecycle follows mcporter `generate-cli` (inspected at [`5362d60`](https://github.com/openclaw/mcporter/blob/5362d60d2e17ca5b2dee3d92f48c0fb7b010abc5/docs/cli-generator.md)): discover at generation, static help, explicit regeneration. Differences: one shared Python runner instead of per-server executables; exact keys with no flag normalization or default insertion.

## 10. Implementation and acceptance

- **C0** (after authorization): pin SDK, confirm caller-context/IPC fit, SDK protocol negotiation, and the result byte cap. Hermetic temp `AVIBE_HOME` and loopback fixtures.
- **C1:** repository + parser, generator/publisher, runtime, CLI, prompt index.
- **C2:** UI/API, i18n, Ruff and UI build; Incus regression on request.

Acceptance:

1. Page and `vibe mcp add` produce identical definitions; Claude-style syntax is parser-tested; add never connects; env/header values live only in owner-private `definition.json` and revision manifests, and are masked in UI/API and never printed by Avibe's own logs.
2. Help/list work with the server and owner down; schema-to-flag contracts hold; injected prompt examples are parser-tested.
3. Generation never publishes partial tool lists; readers never see mixed revisions; identical rebuilds keep the revision; initial generate enables, regenerate preserves state.
4. Calls use `tools/call` only, against the admitted revision; process cap, idle close and timeouts hold; unknown outcomes are not retried.
5. Results keep structured/multimodal/error content; disabled packages reject calls; tests cannot touch real user state.

## 11. Next action

Owner review, then C0 on explicit authorization.
