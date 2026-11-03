/**
 * Form tab of the Add MCP dialog: turns one "URL or command" input plus
 * key/value rows into the same `mcpServers` JSON the paste tab submits.
 *
 * Commands are tokenized shell-style (quotes, backslash escapes) but never run
 * through a shell and never variable-expanded; anything that needs a shell or
 * is ambiguous is rejected with a pointer to the JSON tab.
 */
export type McpTargetIssue = 'invalidUrl' | 'unclosedQuote' | 'shellOperator' | 'windowsPath';

export type McpTarget =
  | { kind: 'empty' }
  | { kind: 'http'; url: string }
  | { kind: 'stdio'; command: string; args: string[] }
  | { kind: 'error'; issue: McpTargetIssue };

export type McpAddTab = 'json' | 'form';

export type McpKeyValueRow = { key: string; value: string };

export type McpAddFormState = {
  name: string;
  target: string;
  envRows: McpKeyValueRow[];
  headerRows: McpKeyValueRow[];
  cwd: string;
};

export const EMPTY_MCP_ADD_FORM: McpAddFormState = { name: '', target: '', envRows: [], headerRows: [], cwd: '' };

const OPERATORS = ['&&', '||', '$(', '|', ';', '>', '<', '`'];
const WINDOWS_PATH = /^[A-Za-z]:[\\/]/;

export function parseMcpTarget(input: string): McpTarget {
  const text = input.trim();
  if (!text) return { kind: 'empty' };
  if (/^https?:\/\//i.test(text)) {
    try {
      const url = new URL(text);
      if (!url.hostname || /\s/.test(text)) return { kind: 'error', issue: 'invalidUrl' };
    } catch {
      return { kind: 'error', issue: 'invalidUrl' };
    }
    return { kind: 'http', url: text };
  }
  const tokens: string[] = [];
  let current: string | null = null;
  let quote: '"' | "'" | null = null;
  let windowsPath = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quote === "'") {
      if (ch === "'") quote = null;
      else {
        if (ch === '\\') windowsPath = true;
        current += ch;
      }
      continue;
    }
    if (quote === '"') {
      if (ch === '"') quote = null;
      else if (ch === '\\' && i + 1 < text.length && '"\\$`'.includes(text[i + 1])) current += text[++i];
      else {
        if (ch === '\\') windowsPath = true;
        current += ch;
      }
      continue;
    }
    if (/\s/.test(ch)) {
      if (current !== null) tokens.push(current);
      current = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      current = current ?? '';
      continue;
    }
    if (OPERATORS.some((op) => text.startsWith(op, i))) return { kind: 'error', issue: 'shellOperator' };
    if (ch === '\\') {
      // A bare backslash followed by an ordinary path character reads as a Windows path.
      const next = text[i + 1];
      if (next === undefined || /[A-Za-z0-9_.-]/.test(next)) windowsPath = true;
      else {
        current = (current ?? '') + next;
        i += 1;
        continue;
      }
    }
    current = (current ?? '') + ch;
  }
  if (quote) return { kind: 'error', issue: 'unclosedQuote' };
  if (current !== null) tokens.push(current);
  if (windowsPath || tokens.some((token) => WINDOWS_PATH.test(token))) return { kind: 'error', issue: 'windowsPath' };
  const [command, ...args] = tokens;
  return { kind: 'stdio', command, args };
}

/** The map a key/value editor holds: rows with a key, keys trimmed, values exactly as typed. */
export function mcpKeyValueMap(rows: McpKeyValueRow[]): Record<string, string> {
  return Object.fromEntries(rows.filter((row) => row.key.trim()).map((row) => [row.key.trim(), row.value]));
}

/**
 * What the Configure dialog sends: each map that has rows replaces the stored one, an empty
 * editor leaves its stored map alone. Null while nothing is entered or a named row has no value,
 * since stored values are never shown and a blank one would silently overwrite a secret.
 */
export function mcpConfigurePayload(envRows: McpKeyValueRow[], headerRows: McpKeyValueRow[]): { env?: Record<string, string>; headers?: Record<string, string> } | null {
  const keyed = [...envRows, ...headerRows].filter((row) => row.key.trim());
  if (keyed.length === 0 || keyed.some((row) => row.value === '')) return null;
  const env = mcpKeyValueMap(envRows);
  const headers = mcpKeyValueMap(headerRows);
  return {
    ...(Object.keys(env).length ? { env } : {}),
    ...(Object.keys(headers).length ? { headers } : {}),
  };
}

export function buildMcpFormJson({
  name, target, envRows, headerRows, cwd,
}: Omit<McpAddFormState, 'target'> & { target: Extract<McpTarget, { kind: 'http' | 'stdio' }> }): string {
  // Only the detected kind's rows are sent: stdio env often holds secrets that must never become remote headers.
  const rows = target.kind === 'http' ? headerRows : envRows;
  const pairs = mcpKeyValueMap(rows);
  const hasPairs = Object.keys(pairs).length > 0;
  const server: Record<string, unknown> = target.kind === 'http'
    ? { type: 'http', url: target.url, ...(hasPairs ? { headers: pairs } : {}) }
    : {
      command: target.command,
      args: target.args,
      ...(hasPairs ? { env: pairs } : {}),
      ...(cwd.trim() ? { cwd: cwd.trim() } : {}),
    };
  return JSON.stringify({ mcpServers: { [name.trim()]: server } }, null, 2);
}

/** The text the Add MCP dialog submits for its active tab, or null while that tab cannot submit. */
export function mcpAddPayload(tab: McpAddTab, text: string, form: McpAddFormState): string | null {
  if (tab === 'json') return text.trim() ? text : null;
  const target = parseMcpTarget(form.target);
  if (!form.name.trim() || (target.kind !== 'http' && target.kind !== 'stdio')) return null;
  return buildMcpFormJson({ ...form, target });
}
