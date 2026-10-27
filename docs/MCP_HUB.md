# MCP Hub

MCP Hub turns an MCP server into `vibe mcp` commands that every Agent backend can call.

Hub uses the MCP servers you already configured for your coding agents, and keeps them in those same files:

| Backend | File | Key |
|---|---|---|
| Claude Code | `~/.claude.json` | `mcpServers` |
| Codex | `~/.codex/config.toml` | `[mcp_servers.<name>]` |
| OpenCode | `~/.config/opencode/opencode.json` | `mcp` |

If a name appears in more than one file, the first file in this table is the one Hub reads and edits. Avibe sessions do not load these servers natively: Claude, Codex and OpenCode started by Avibe call MCP tools only through `vibe mcp`. Your files are not changed for this, and the agents behave as before when you run them yourself.

## Add a server

On the Skills page, paste an `mcpServers` block under **MCP Hub**, then choose **Parse and preview** and **Save**:

```json
{
  "mcpServers": {
    "docs": {"type": "http", "url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer <token>"}},
    "local-tools": {"command": "/absolute/path/to/server", "args": ["--read-only"]}
  }
}
```

The same works from the CLI, using a subset of Claude Code's `mcp add` syntax:

```bash
vibe mcp add --transport http docs https://example.invalid/mcp --header 'Authorization: Bearer <token>'
vibe mcp add local-tools --env MODE=readonly -- python server.py
vibe mcp add --json-stdin < servers.json
```

- Supported transports are stdio and Streamable HTTP. SSE and OAuth are not supported.
- A new server is written to `~/.claude.json`, or to `~/.codex/config.toml` when it sets `cwd` (only Codex supports `cwd`).
- To change an existing server, save it again with **Replace existing servers** (or `--replace`). The server is updated in the file it already lives in.
- `vibe mcp remove <name>` deletes the server from every one of these files.
- A `cwd` you enter in Hub (page or `vibe mcp add`) must be absolute. Without one, the server runs in the Agent default directory.
- A relative `cwd` already in a backend config file (for example `cwd = "."` in `~/.codex/config.toml`) is resolved against that file's directory, for every backend. A relative command such as `./server` then runs from that directory.
- Native extras Hub does not use (timeouts, native `enabled`, …) are ignored, except Codex `env_vars`: those names are copied from the parent environment at read/generate. `${VAR}` placeholders are still not expanded.
- A Codex/OpenCode server with `enabled = false` is marked as disabled there, but Hub can still generate and call it; that switch only controls the backend's own loading.
- If you delete a generated server from the native file, Hub keeps the published package and marks it deleted in that backend until you run `vibe mcp remove`.
- `${VAR}` placeholders are not expanded. Enter the literal value; an existing entry that uses one is listed with an error.

Saving never runs or connects to the server.

## Credentials are plaintext

Env and header values are stored as given in the backend config file, exactly as each coding agent stores them. The Web UI and API never return these values; when you edit a server, leaving a value blank keeps the stored one. URLs with `user:pass@` are rejected; put credentials in an `Authorization` header instead.

## Generate and refresh

**Generate** (`vibe mcp generate <name>`) connects once, lists every tool, and publishes a static package. The first generation also enables the server. The Hub page labels a later publish **Refresh**; the CLI verb stays `vibe mcp regenerate <name>` and keeps the enabled state.

Edits to a server's definition take effect only after you refresh. Until then the page shows **Pending changes**. If a tool's schema changed on the server, calls report an error that suggests refreshing.

Enable, disable or delete a server from the page, or with `vibe mcp enable|disable|remove <name>`. Only the instance Owner can add, generate, enable, disable or remove servers.

## Call tools

Agents see enabled servers in their system prompt and discover tools on demand:

```bash
vibe mcp --list                       # enabled servers (--all includes the rest)
vibe mcp docs --list                  # tools, read from the published package
vibe mcp docs search --help           # flags for one tool
vibe mcp docs search --query 'async Python' --limit 5
vibe mcp docs search --args-json '{"query": "async Python", "limit": 5}'
```

Lists and help work offline. A call prints one JSON envelope to stdout and exits with:

- `0`: success
- `2`: usage or argument error
- `1`: any other failure; `error.type` names it

Never retry a call that reports `unknown_outcome` blindly: the tool may already have acted.
