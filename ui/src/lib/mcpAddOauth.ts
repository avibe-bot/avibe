export type McpOauthMode = 'direct' | 'paste';

export type McpAddOauthAction =
  | { kind: 'assign'; name: string }
  | { kind: 'list'; names: string[] }
  | { kind: 'none' };

/** After a successful add, decide same-tab start vs a result list of login links. */
export function mcpAddOauthAction(input: {
  names: string[];
  failedCount: number;
  oauthMode: McpOauthMode;
  desktopShell: boolean;
}): McpAddOauthAction {
  const names = input.names.filter(Boolean);
  if (names.length === 0) return { kind: 'none' };
  if (names.length === 1 && input.failedCount === 0 && input.oauthMode === 'direct' && !input.desktopShell) {
    return { kind: 'assign', name: names[0] };
  }
  return { kind: 'list', names };
}
