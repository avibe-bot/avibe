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

/** `empty` marks a value the user explicitly chose to leave empty (Configure dialog only). */
export type McpKeyValueRow = { key: string; value: string; empty?: boolean };

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

/** One map of the Configure dialog: untouched until the user edits its rows or explicitly clears it. */
export type McpConfigureMap = { rows: McpKeyValueRow[]; edited: boolean; cleared: boolean };

export const mcpConfigureMap = (rows: McpKeyValueRow[] = []): McpConfigureMap => ({ rows, edited: false, cleared: false });

type ConfigureMapPayload = Record<string, string> | undefined | null;

function configureMapPayload(map: McpConfigureMap): ConfigureMapPayload {
  if (map.cleared) return {};
  const keyed = map.rows.filter((row) => row.key.trim());
  // Removing every row is not a clear; only the explicit Clear action sends an empty map.
  if (!map.edited || keyed.length === 0) return undefined;
  // Stored values are never shown, so a blank value counts only when the user chose "empty value".
  if (keyed.some((row) => row.value === '' && !row.empty)) return null;
  return mcpKeyValueMap(keyed.map((row) => (row.empty ? { ...row, value: '' } : row)));
}

/**
 * What the Configure dialog sends: an edited map replaces the stored one, a cleared map is sent
 * as `{}`, and an untouched map is omitted so its stored values stay. Null while nothing would be
 * sent or an edited row has a blank value the user did not explicitly choose.
 */
export function mcpConfigurePayload(env: McpConfigureMap, headers: McpConfigureMap): { env?: Record<string, string>; headers?: Record<string, string> } | null {
  const envMap = configureMapPayload(env);
  const headerMap = configureMapPayload(headers);
  if (envMap === null || headerMap === null || (envMap === undefined && headerMap === undefined)) return null;
  return { ...(envMap ? { env: envMap } : {}), ...(headerMap ? { headers: headerMap } : {}) };
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
