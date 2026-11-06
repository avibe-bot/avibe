# OpenCode V2 global MCP import

Contract: the native importer reads both legacy `mcp.<name>` and V2
`mcp.servers.<name>`, respects disabled servers, and deletes exactly the selected
entry without reformatting other content. A refused edit preserves publication.
No project discovery or backend startup policy changes belong to this PR.

## Evidence

Installed OpenCode 2.0.32, `/Users/rk/.opencode/bin/opencode`, was run with
isolated HOME and all XDG directories and no inherited credential environment:
`opencode mcp add --global same -- /usr/bin/false GLOBAL`.
It wrote the shape in `tests/fixtures/mcp_native/opencode-v2.json` to isolated
`$XDG_CONFIG_HOME/opencode/opencode.json`. Fixture indentation is normalized;
command and container structure are preserved. The CLI output reported the
server added to that isolated file. No user native config was accessed.

[Official V2 MCP documentation](https://opencode.ai/v2/docs/mcp-servers)
defines `mcp.servers` and `disabled`; legacy configs use direct `mcp` entries
and `enabled`. Before this fix the fixture becomes one invalid server called
`servers`, with `Unsupported OpenCode MCP type None.`

## Validation ownership

- Native read boundary: parameterized CLI-shaped fixture and V1 equivalent,
  including disabled entries and a legitimate V1 server named `servers`.
  Existing direct-map fixtures miss the V2 container regression.
- Native delete boundary: nested JSONC deletion preserves surrounding bytes,
  sibling entries and MCP settings, including removal of the last server.
  Existing tests cover only top-level containers.
- Runtime remove boundary: extend the existing refusal/publication/OAuth test
  to a V2 source. This protects runtime retirement ordering, distinct from
  text mutation, without duplicating the test.

The existing global file selection and precedence remain unchanged. Avibe never
adds entries to these files. Nested deletion extends the existing JSONC helper;
there is no second parser or configuration framework.
