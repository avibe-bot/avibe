# MCP Hub

MCP Hub turns an MCP server into `vibe mcp` commands that every Agent backend can call.

Reachable servers are registered automatically. Two sources share one list:

| Source | Where it lives |
|---|---|
| Avibe | `<avibe-home>/mcp/<name>/definition.json` (page or `vibe mcp-add`) |
| Claude | `~/.claude.json` `mcpServers` (read-only) |
| Codex | `~/.codex/config.toml` `[mcp_servers.<name>]` (read-only) |
| OpenCode | `~/.config/opencode/opencode.json` `mcp` (read-only) |

Avibe sessions do not load native MCP: Claude, Codex and OpenCode started by Avibe call tools only through `vibe mcp`. Native files are not rewritten for this.

## Discover

Controller startup runs one discovery pass in the background. To run it now:

```bash
vibe mcp-discover
```

Success is listed. Failure is omitted; `vibe mcp-discover` prints the reason. Concurrent passes join.

Refresh one server:

```bash
vibe mcp docs --refresh
```

A Codex/OpenCode server with `enabled = false` is omitted. Native extras Hub does not use (timeouts, …) are ignored, except Codex `env_vars` copied from the parent environment at read. A relative native `cwd` resolves against that file's directory.

## Add a server

On the Skills & MCP page, MCP tab, paste an `mcpServers` block and choose **Add MCP**. That writes only Avibe's store, then refreshes the name.

```bash
vibe mcp-add --transport http docs https://example.invalid/mcp --header 'Authorization: Bearer <token>'
vibe mcp-add local-tools --env MODE=readonly -- python server.py
vibe mcp-add --json-stdin < servers.json
```

- Supported transports are stdio and Streamable HTTP. SSE is not supported. HTTP servers that advertise OAuth can be logged in from the Skills & MCP page or with `vibe mcp <name> --login`.
- Add never writes Claude/Codex/OpenCode files.
- A Hub-entered `cwd` must be absolute.
- `${VAR}` placeholders are not expanded.

## Delete

- Avibe source: remove the definition and artifacts.
- Native source: hide in Avibe (`<avibe-home>/mcp/.hidden.json`); the native file is unchanged.

```bash
vibe mcp docs --remove
```

## OAuth login

HTTP definitions may include Claude-format OAuth options such as `"oauth": {"scopes": "offline_access"}`. When a server requires OAuth it remains visible as **needs login**, but its tools are not added to the Agent prompt until login succeeds.

Use **Log in** in the Skills & MCP page, or:

```bash
vibe mcp suno --login
vibe mcp suno --login-complete '<callback URL>'
vibe mcp suno --logout
```

Avibe uses a direct callback for HTTPS and loopback HTTP. LAN or other plain-HTTP access uses paste mode: authorize in the new tab, copy the callback URL from the address bar, and paste it into the dialog or `--login-complete`. Tokens and the dynamic client registration are stored in `<avibe-home>/mcp/<name>/oauth.json`, an owner-private plaintext file with mode `0600`; it is never shown in lists, help, API responses, or logs. Removing or logging out deletes it.


Env and header values are stored as given. URLs with `user:pass@` are rejected; put credentials in an `Authorization` header.

## Call tools

Agents see registered servers in their system prompt and discover tools on demand. Each server's line shows the description the server itself reports, or else the first sentence of its instructions, captured when Avibe refreshes it; a server with neither is listed by name only. The list is paged like Skills (25 servers per page); the prompt shows page 1 and `vibe mcp --list --page N` shows the rest:

```bash
vibe mcp --list
vibe mcp --list --page 2
vibe mcp docs --list
vibe mcp docs search --help
vibe mcp docs search --query 'async Python' --limit 5
vibe mcp docs search --args-json '{"query": "async Python", "limit": 5}'
```

Lists and help work offline. A call prints one JSON envelope to stdout.

Never retry a call that reports `unknown_outcome` blindly: the tool may already have acted.

Only the instance Owner can add, discover, refresh or remove servers.
