# MCP Hub

MCP Hub turns an MCP server into `vibe mcp` commands that every Agent backend can call. No backend-native MCP configuration is involved.

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
- To change an existing server, save it again with **Replace existing servers** (or `--replace`).
- `cwd` must be an absolute path. Without one, the server runs in the Agent default directory.
- `${VAR}` placeholders are not expanded; enter the literal value.

Saving never runs or connects to the server.

## Credentials are plaintext

Env and header values are stored as given, in owner-private files under `~/.avibe/mcp/<name>/`. This matches Claude Code's own MCP configuration. The Web UI and API never return these values; when you edit a server, leaving a value blank keeps the stored one. URLs with `user:pass@` are rejected; put credentials in an `Authorization` header instead.

## Generate and regenerate

**Generate** (`vibe mcp generate <name>`) connects once, lists every tool, and publishes a static package. The first generation also enables the server. **Regenerate** (`vibe mcp regenerate <name>`) publishes a fresh package and keeps the enabled state.

Edits to a server's definition take effect only after you regenerate. Until then the page shows **Pending changes**. If a tool's schema changed on the server, calls report an error that suggests regenerating.

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
