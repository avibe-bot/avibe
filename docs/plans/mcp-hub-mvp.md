# Avibe MCP Hub MVP design

> Status: implemented (instance-global Hub). Official latest stable Python MCP SDK, no OAuth, no generated Skills.
> Model: auto-discover reachable servers and register them; no draft/enable lifecycle.
> Principle: smallest complete design; reuse existing caller context, IPC, credentials, i18n and owner lifecycle; no speculative defenses.
> Design source of truth: `avibe-docs/design.pen`, frames `avibe — Skills & MCP · …`.

## 1. Change contract

Add MCP Hub to the Skills & MCP page. Reachable servers are registered automatically; unreachable ones are simply not listed. Every backend calls tools through `vibe mcp <server> <tool>`; no Skill loader.

**Native MCP is off.** Claude, Codex and OpenCode must not load their own MCP servers in Avibe sessions. Hub **reads** native files as one source and stores Avibe-added servers only under `<avibe-home>/mcp/<name>/definition.json` (add never writes native files):

- Claude: `~/.claude.json` `mcpServers`
- Codex: `~/.codex/config.toml` `[mcp_servers.<name>]`
- OpenCode: `~/.config/opencode/opencode.json` `mcp`

Avibe-only runtime artifacts (published CLI revision, current pointer) live under `<avibe-home>/mcp/<name>/`. Disable native loading with `strict_mcp_config=True` + empty `mcp_servers` for Claude, Codex `-c enabled=false` overrides, and OpenCode overlay at spawn — never delete the user's files.

```text
startup / vibe mcp-discover → connect + tools/list → publish revision (success = listed)
vibe mcp-add → Avibe store + refresh that name
```

In scope: stdio and Streamable HTTP, auto-discovery, plaintext env/header configuration, static help/list, shared runtime, structured/multimodal results.

Out of scope: OAuth, SSE (explicit `unsupported_transport_sse` error), project scope, Skill generation, resources/prompts, sampling, update detection, auto-install/upgrade of server software, package sharing/import.

## 2. Package on disk

```text
<avibe-home>/mcp/
  .hidden.json             # names hidden from Avibe lists (native files unchanged)
  <name>/
    definition.json        # Avibe-owned connection (absent for native-only servers)
    current.json           # pointer to the one published snapshot
    revisions/<revision>/  # <revision> = generation timestamp + random suffix
      definition.json      # snapshot used to connect
      manifest.json        # formatVersion, description, generatedAt, serverInfo.version
      tools.json
      commands.json
```

- Native files are a read-only source. Avibe-added servers store `definition.json` here. Hub never writes Claude/Codex/OpenCode files. Every file is written with `config/atomic_io.py` `write_atomic`.
- `commands.json` is declarative data interpreted by the shared `vibe mcp` runner, not code.
- One published snapshot per server. An unreadable or unknown-`formatVersion` snapshot omits only that package; leftover MVP `state.json` is ignored.

## 3. Prompt injection

The shared system prompt gets a short discovery index (outside `core/managed_skills.py`), only when at least one server is registered:

```text
## MCP capabilities

Use `vibe mcp <server> --list` to discover available tools.
Use `vibe mcp <server> <tool> --help` to inspect arguments before calling.
Call tools with `vibe mcp <server> <tool> [arguments]`.
Help and lists read local snapshots; tool calls access the MCP server.

Available servers:
- context7: Find current library documentation and code examples.
- github: Work with GitHub repositories, issues, and pull requests.

List all servers: `vibe mcp --list`.
```

- One line per package, sorted by name; description sanitized to one line, max 160 chars.
- Description: from the published snapshot's manifest, chosen at refresh and never read live: the user-provided `--description` / paste description, else the server's initialize `serverInfo.description`, else `serverInfo.title`, else the fixed English `Tools from an MCP server.` (owner decision; the prompt is English, so the fallback is not localized). There is one snapshot per server; there are no pending edits.
- List at most 50 servers; if more exist, append the omitted count and `vibe mcp --list`.
- No tool names, schemas, URLs, commands, credentials or management commands.
- The block has two fixed parts: stable shared instructions (examples must stay parser-valid), then one `<server>: <description>` line per package, where `<server>` is the exact CLI selector.
- Disable/remove takes effect on the next prompt build; new calls are rejected immediately even if a running turn still sees the old list.
- The index grants nothing: every call rechecks the published revision at the owner.

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
vibe mcp --list                         # registered servers
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

`vibe mcp` is reserved for per-server sub-CLIs. Management is:

```bash
vibe mcp-add --transport http docs https://example.invalid/mcp --header 'Accept-Language: en'
vibe mcp-add playwright -- npx -y @playwright/mcp@latest
vibe mcp-add --json-stdin
vibe mcp-discover
vibe mcp docs --refresh
vibe mcp docs --remove
```

- `vibe mcp-add` uses the Claude Code `mcp add` subset plus `--description`, `--cwd`, `--replace`, `--json-stdin`. It writes only the Avibe store, then refreshes that name; the call returns after refresh.
- `vibe mcp-discover` runs one full pass (joins an in-flight pass) and prints registered / failed + reason. Controller startup runs the same pass in the background, off the controller loop.
- Names: empty, `.`/`..`, NUL, `/`/`\\` rejected. Management verbs are not reserved names.
- Owner-only: mcp-add, mcp-discover, `--refresh`, `--remove`. Editor: list/help/call.

## 5. Page configuration

Skills & MCP page, MCP tab. No status badges, enable toggle, or edit dialog.

- Toolbar: 技能 | MCP, search, `+ 添加 MCP`. Header `刷新` = discover all.
- Row: transport icon, name, command/URL, tool count · transport, source chip (Avibe/Claude/Codex/OpenCode).
- Detail: connection, tools, `vibe mcp <name> <tool>`, last refreshed. Footer: 刷新 / 删除.
- Add dialog: JSON textarea, 取消 / 添加. Invalid JSON or failed refresh keeps the dialog open.
- Delete: Avibe source removes artifacts. Native source **hides in Avibe only** (`.hidden.json`); the native file is never touched. Owner-confirmed.

## 6. Discovery and versions

- Discovery = refresh every Avibe definition and every native source entry (except natively `enabled=false` and Avibe-hidden names). Connect, list tools, publish one snapshot. Success → registered. Failure → not listed; reason in logs and `vibe mcp-discover`.
- Avibe name wins over a native duplicate.
- A native entry removed or set `enabled=false` disappears at the next discovery; leftover artifacts may be deleted then.
- Publish = write one snapshot directory, then atomically replace `current.json`. A failed refresh unlists the name (`current.json` dropped); revision files may remain for a later retry.
- Concurrent discover passes join one in-flight task.
- Keep exactly one published snapshot per server. Tools with `execution.taskSupport: "required"` are skipped with a diagnostic.

## 7. Runtime

- **Owner:** the Controller, over existing control IPC. UI and CLI hold no runtimes.
- **Identity:** existing caller context (`core/caller_context.py`, `AVIBE_SESSION_ID`, …) as used by `vibe task/watch/agent`. No MCP-specific token.
- **Permission:** instance-global. The local OS user has full rights. Management commands (add, discover, refresh, remove) are Owner-only through the existing CLI command admission, because refreshing a stdio definition runs an arbitrary local command; list/help (admission runs before any help is rendered) and tool calls use the same floor as using Skills (Editor), matching who sees the prompt index. Remote-context callers thus get exactly the permissions they have elsewhere. No MCP-specific policy.
- **Connections:** one client connection per (revision, caller session) for every transport — a stdio process, or an HTTP client holding its own `Mcp-Session-Id` (closed with the connection). This matches Claude Code and Codex (one client per session). Context-free calls are one-shot. Calls within one session are serialized on its connection. A connection whose call ends with `unknown_outcome` is closed, never reused. An HTTP `Session not found` (pre-dispatch) closes the client and the call is retried once on a fresh connection.
- **Limits** (constants): at most 8 stdio processes in total, including refresh; batch add/discover wait for a slot, a single tool call fails with `capacity` at the cap. Idle close 10 min; call timeout 120 s; refresh 5 min. HTTP is not capped.
- Servers that lock a shared resource (e.g. Playwright's default persistent profile) need their own isolation flag in the provider config (`--isolated`); Avibe does not rewrite server argv.
- **Schemas at call time:** the CLI parses and validates against the published snapshot only. A new runtime connection lists all `tools/list` pages through the SDK before its first call (same 100-page cap as refresh), so the SDK knows every tool's output schema and header annotations; that list never changes the published package.
- **Calls:** the owner checks a published snapshot, connects or reuses, sends `tools/call` and returns the result with the revision used. Elicitation/sampling/roots are out of scope: the client's callbacks for them reject; since the tool may already have acted, the call ends as `unknown_outcome`. The owner fixes the revision at admission and validates arguments against it.
- **Remove/refresh:** remove blocks new calls; remove and a successful refresh close the package's superseded idle connections at once (busy ones close when their call settles); in-flight work may finish or report unknown outcome.
- **Event loop:** all MCP SDK sessions run on one dedicated runtime thread with its own event loop, owned by the Controller; the SDK's synchronous result validation therefore never blocks the Controller loop. All other MCP work — argument validation, refresh, artifact I/O, result envelope serialization and sizing — also runs off the Controller loop; the Controller loop only hands IPC requests over, per the repo's controller-loop rules.

Result envelope:

```json
{"schemaVersion": 1, "server": "docs", "revision": "<hex>", "tool": "search", "status": "ok",
 "result": {"content": [], "isError": false}}
```

Preserve typed content, `structuredContent`, `isError` and explicit null/false/empty values. Results above a fixed byte cap (set in C0; no shared IPC cap exists) are not returned: the call still completed, so the envelope keeps `isError` and reports `resultOmitted: "too_large"` — success, or the normal tool error when `isError` is true — so Agents never retry a committed call.

## 8. UI / API

No draft/generated/error/pending/nativeDeleted badges. No enable toggle.

```text
GET    /api/mcp
GET    /api/mcp/{name}
POST   /api/mcp                  # add JSON block → Avibe store + refresh
POST   /api/mcp/discover
POST   /api/mcp/{name}/refresh
DELETE /api/mcp/{name}           # Avibe: remove; native: hide
```

Read routes Editor; management Owner. Native async FastAPI → owner IPC.

## 9. Prior art

CLI shape follows mcporter `generate-cli` (inspected at [`5362d60`](https://github.com/openclaw/mcporter/blob/5362d60d2e17ca5b2dee3d92f48c0fb7b010abc5/docs/cli-generator.md)): discover once, static help, explicit refresh. Differences: one shared Python runner instead of per-server executables; exact keys with no flag normalization or default insertion; one published snapshot, no draft/enable lifecycle.

## 10. Implementation and acceptance

- **C0** (after authorization): pin SDK, confirm caller-context/IPC fit, SDK protocol negotiation, and the result byte cap. Hermetic temp `AVIBE_HOME` and loopback fixtures.
- **C1:** repository + parser, publisher, runtime, CLI, prompt index.
- **C2:** UI/API, i18n, user documentation (adding servers, plaintext credential behavior, refresh, tool invocation), Ruff and UI build; Incus regression on request.
- **C3:** Disable Claude/Codex/OpenCode native MCP in Avibe sessions. Hub **reads** those backends' config files as a source and stores Avibe-added servers under `<avibe-home>/mcp/`. Spawn isolation is unchanged.

Acceptance:

1. Fresh start with reachable native servers lists and calls them without user action; unreachable ones are absent and `vibe mcp-discover` shows why.
2. `vibe mcp-discover` and `vibe mcp <name> --refresh` work from CLI and UI; concurrent passes join. Startup discovery never blocks the controller loop or readiness.
3. Add through page/`vibe mcp-add` writes only the Avibe store and the server appears after the call returns.
4. Delete of an Avibe server removes it. Delete of a native server hides it in Avibe only (owner-confirmed); the native file is never touched.
5. Help/list work with the owner down; schema-to-flag contracts hold; injected prompt examples are parser-tested.
6. UI matches the Pencil frames; leftover MVP `state.json` enabled flags and drafts load safely (ignored).
7. Avibe-spawned sessions do not load native MCP (C3 unchanged).

## 11. Next action

Owner review, then C0 on explicit authorization.
