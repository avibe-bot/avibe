/** True when paste includes an HTTP MCP server that Hub can OAuth-login. */
export function pasteMayNeedOAuthLogin(text: string): boolean {
  try {
    const parsed: unknown = JSON.parse(text);
    if (!parsed || typeof parsed !== 'object') return false;
    const servers = (parsed as { mcpServers?: unknown }).mcpServers;
    const entries =
      servers && typeof servers === 'object' && !Array.isArray(servers)
        ? Object.values(servers as Record<string, unknown>)
        : [parsed];
    return entries.some(isOAuthEligiblePasteServer);
  } catch {
    return false;
  }
}

function isOAuthEligiblePasteServer(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const server = value as Record<string, unknown>;
  const url = typeof server.url === 'string' && server.url.trim();
  if (!url || server.command) return false;
  const headers = server.headers;
  if (!headers || typeof headers !== 'object' || Array.isArray(headers)) return true;
  return !Object.keys(headers as Record<string, unknown>).some((key) => key.toLowerCase() === 'authorization');
}

export function preparedTabNeedsAuthorizeFallback(tab: Window | null): boolean {
  return tab == null || tab.closed;
}
