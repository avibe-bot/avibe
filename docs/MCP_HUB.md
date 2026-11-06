# MCP Hub

MCP Hub turns an MCP server into `vibe mcp` commands that every Agent backend can call.

Reachable servers are registered automatically. Two sources share one list:

| Source | Where it lives |
|---|---|
| Avibe | `<avibe-home>/mcp/<name>/definition.json` (page or `vibe mcp-add`) |
| Claude | `~/.claude.json` `mcpServers` |
| Codex | `~/.codex/config.toml` `[mcp_servers.<name>]` |
| OpenCode | `~/.config/opencode/opencode.json` `mcp` |

Avibe sessions do not load native MCP: Claude, Codex and OpenCode started by Avibe call tools only through `vibe mcp`. Native files are not rewritten for this.

## Discover

Controller startup runs one discovery pass in the background. To run it now:

```bash
vibe mcp-discover
```

Avibe-owned definitions remain listed if discovery fails, with no callable tools. Native discovery failures are omitted; `vibe mcp-discover` prints the reason. A discovery requested from a project directory refreshes both the global candidates and that project's approved, configured servers. Concurrent global passes join.

Refresh one server:

```bash
vibe mcp docs --refresh
```

OpenCode global import accepts legacy `mcp.<name>` and V2 `mcp.servers.<name>` configurations. A Codex/OpenCode server with `enabled = false`, or an OpenCode server with `disabled: true`, is omitted. Native extras Hub does not use (timeouts, …) are ignored, except Codex `env_vars` copied from the parent environment at read. A relative native `cwd` resolves against that file's directory.

## Add a server

On the Skills page, MCP tab, paste an `mcpServers` block and choose **Add MCP**. That writes only Avibe's store, then refreshes the name.

The Add MCP dialog has two tabs. **Paste JSON** (the default) accepts any `mcpServers` block. **Form** covers the common cases:

- **Name** — validated like any other add; a rejected name shows the server's error.
- **URL or command** — input starting with `http://` or `https://` becomes a Streamable HTTP server (a `/sse` URL is kept as typed). Anything else is a stdio command, split shell-style: quotes and backslash escapes are honored, but nothing runs through a shell and `$VARS` are not expanded. Shell operators (`|`, `&&`, `||`, `;`, `>`, `<`, backticks, `$(`), unclosed quotes and Windows paths are rejected; use the JSON tab for those. `docker run -e KEY=VAL ...` keeps `-e` in the arguments.
- **Headers** (HTTP) or **Environment variables** (stdio) — key/value rows, stored in plaintext like JSON values.
- **Advanced** (stdio) — working directory.

OAuth settings, timeouts, `enabled`, tool filters, `${VAR}` references, `envFile`, `inputs`, and `headersHelper` are JSON-only. Each tab keeps its input while you switch.

```bash
vibe mcp-add --transport http docs https://example.invalid/mcp --header 'Authorization: Bearer <token>'
vibe mcp-add local-tools --env MODE=readonly -- python server.py
vibe mcp-add --json-stdin < servers.json
```

- Supported transports are stdio and Streamable HTTP. SSE is not supported. HTTP servers that advertise OAuth can be logged in from the Skills page or with `vibe mcp <name> --login`.
- Add never writes Claude/Codex/OpenCode files.
- A Hub-entered `cwd` must be absolute.
- Global Avibe and project servers accept `${VAR}` in URL, env values and header values, never command or args. Variables come only from that server's private configuration, not the process environment.
- Unconfigured definitions are saved and show **needs config** with missing variable names. Use **Configure** or `vibe mcp <name> --configure '{"env": {"TOKEN": "<value>"}}'`. A supplied map replaces that map (an empty map clears it); omitted maps stay unchanged. Only the Owner can configure.
- Templates stay in the definition. Concrete env/header values supplied through Avibe are stored in private `secrets.json`; add/replace moves each supplied key to the correct destination and preserves unspecified keys. Native imports retain their existing semantics.
- After an accepted add/replace or configuration change, tools become callable only after successful discovery. If discovery fails, the saved definition remains visible for Configure, Refresh or Remove. Calls already admitted may finish; new admissions are serialized with the mutation. This is not a guarantee of an old OAuth-secret snapshot.

## Delete

- Avibe source: remove the definition and artifacts.
- Native source: delete that one entry from the Claude/Codex/OpenCode file listed above (Codex: the `[mcp_servers.<name>]` table and its sub-tables). Everything else in the file — other servers, keys, comments, layout, file mode — is kept. That agent no longer has the server either. If the entry is already gone, only Avibe's snapshot is dropped; if the file cannot be parsed, or the entry is not a standalone block (for example an inline table), nothing is written and the error says to remove it by hand. Names hidden by older versions (`<avibe-home>/mcp/.hidden.json`) stay hidden; new deletes do not write it.

```bash
vibe mcp docs --remove
```

## Project servers

A project can keep MCP servers in `<project root>/.mcp.json`, in Claude Code's `mcpServers` format. The project root is the one project Skills use: the run target's project base, else the nearest `.git` ancestor, else the working directory.

- **Scope.** A Session, `vibe mcp` and the prompt see global servers plus the servers of their own project's `.mcp.json`; never another project's. A project server shadows a global one with the same name. `vibe mcp --global ...` uses only global servers. In the Web MCP page's Project view, the project's own servers are listed first under "Project MCP", then the inherited global servers under "Also available · inherited from global" (view-only there; manage them in the Global tab). A global server shadowed by a same-name project server is not listed in the inherited group.
- **Approval.** Every project server, stdio or HTTP, is **pending approval** until the instance Owner approves it: it does not start, connect, appear in the prompt, or accept calls. Approval binds to the raw `.mcp.json` entry, so any edit to that entry needs approval again; changing a secret does not.
- **Secrets.** Write `${VAR}` in `url`, env values or header values (not in `command` or `args`). All placeholders of a server share one set of variables, supplied with `--configure` and stored outside the project. Until every variable is set, the server shows **needs config** and the missing names.
- **Add.** Project additions through the CLI and Web use the same placeholder-aware structural validation as the project loader; unresolved URL variables remain verbatim in `.mcp.json` and require configuration after approval. Global additions use the same validation and private-value partition. Without `--global`, `vibe mcp-add` writes to the project's `.mcp.json` when the file exists. Avibe writes `type`, `command`, `args`, `url`, `cwd` and the supported `oauth` options (`clientId`, `scopes`); env and header values are classified per key: values containing `${VAR}` are templates written verbatim to the project file; concrete values go only to the private cache. Mixed maps are split accordingly. A supplied key replaces its previous value and is removed from the other destination; unspecified keys are preserved. The complete prospective document and cache are validated before either is written. Replacing an entry keeps the file's own env/headers (including placeholders), `cwd`, `type` and other keys, and preserves the file's existing formatting and indentation (compact files stay compact; a file whose layout Avibe cannot edit in place falls back to a 2-space rewrite). If the file changed while Avibe was writing it, the command fails with `config_changed`, keeps your edit, and can be retried.

```bash
vibe mcp github --approve
vibe mcp github --configure '{"env": {"GITHUB_TOKEN": "<token>"}}'
vibe mcp github --configure '{"headers": {"Authorization": "Bearer <token>"}}'
```

`--configure` replaces the `env` or `headers` map it is given and keeps the other. Invalid names or values are rejected and the stored secrets stay as they were. Approve, configure and remove are Owner-only.

`vibe mcp --list` shows blocked servers with the command that unblocks them, for example `- github: pending approval — run vibe mcp github --approve`.

Removing the last project server of a project also removes its now-empty `<key>` directory. Secrets, approval, OAuth tokens and published tools live in `<avibe-home>/project_mcp/<key>/<name>/` (owner-only `0700` directories, `0600` files). `<key>` is the Avibe Project id, or `dir-<hash>` for a folder that is not a Project.

Limitations:

- Avibe does not inspect or clean values you type into `.mcp.json`. Anything written there, including a literal token, is in your repository; use placeholders for secrets.
- Deleting an entry from `.mcp.json` by hand hides the server at once, but its cached secrets, approval and tokens stay. Do not run `vibe mcp <name> --remove` afterwards: with the entry gone, the name resolves to the global server of that name, which would be removed instead, and the project cache would stay. To drop the cache, restore the entry, then run `vibe mcp <name> --remove` from the project. Restoring the same entry also restores its approval and secrets.
- Scopes separate what Agents are shown, not what they can read: Agents run as the same OS user.
- Other native project configs (OpenCode, Codex project config, Claude per-path entries) are not imported.

## OAuth login

HTTP definitions may include Claude-format OAuth options such as `"oauth": {"scopes": "offline_access"}`. When a server requires OAuth it remains visible as **needs login**, but its tools are not added to the Agent prompt until login succeeds.

Use **Log in** in the Skills page, or:

```bash
vibe mcp suno --login
vibe mcp suno --login-complete '<callback URL>'
vibe mcp suno --logout
```

Avibe uses a direct callback for HTTPS and loopback HTTP: the browser stays in the current tab and returns to the MCP page after login. LAN or other plain-HTTP access uses paste mode: authorize in the new tab, copy the callback URL from the address bar, and paste it into the small login dialog or `--login-complete`. Tokens and the dynamic client registration are stored in `<avibe-home>/mcp/<name>/oauth.json`, an owner-private plaintext file with mode `0600`; it is never shown in lists, help, API responses, or logs. Removing or logging out deletes it.


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

Lists and help ask the running Avibe service, which resolves the caller's project; when it is stopped they report `unavailable`. A call prints one JSON envelope to stdout.

Never retry a call that reports `unknown_outcome` blindly: the tool may already have acted.

Only the instance Owner can add, discover, refresh or remove servers.
