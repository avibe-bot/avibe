// Per-backend brand accent (mint / cyan / violet) + labels, shared by the
// Agents and Skills surfaces. Matches design.pen. This is the *accent* of a
// backend; it is intentionally distinct from `lib/agentBackends.ts`, which
// carries Settings-page backend-tile chrome (icons, tile classes, CLI
// defaults) for a different visual context.

export const BACKEND_ORDER = ['claude', 'opencode', 'codex'] as const;
export type Backend = (typeof BACKEND_ORDER)[number];

export const BACKEND_LABEL: Record<Backend, string> = {
  claude: 'Claude',
  opencode: 'OpenCode',
  codex: 'Codex',
};

// Text / icon accent — e.g. <Icon className={BACKEND_TEXT[b]} />.
export const BACKEND_TEXT: Record<Backend, string> = {
  claude: 'text-mint-ink',
  opencode: 'text-cyan-ink',
  codex: 'text-violet-ink',
};

// Solid dot fill — e.g. a status dot inside a chip.
export const BACKEND_DOT: Record<Backend, string> = {
  claude: 'bg-mint',
  opencode: 'bg-cyan',
  codex: 'bg-violet',
};

// Full pill surface (soft bg + 40% border + accent text) for backend chips.
export const BACKEND_CHIP: Record<Backend, string> = {
  claude: 'bg-mint-soft border-mint/40 text-mint-ink',
  opencode: 'bg-cyan-soft border-cyan/40 text-cyan-ink',
  codex: 'bg-violet-soft border-violet/40 text-violet-ink',
};

export function isBackend(value: string): value is Backend {
  return (BACKEND_ORDER as readonly string[]).includes(value);
}

// askill agent id (e.g. "claude-code") -> our backend id. Agents we don't
// surface (cursor, windsurf, …) are intentionally absent.
export const AGENT_ID_TO_BACKEND: Record<string, Backend> = {
  'claude-code': 'claude',
  opencode: 'opencode',
  codex: 'codex',
};

// Distinct backends a skill serves, in canonical order, from its linked
// askill agents. Unknown/unsupported agents are dropped.
export function backendsFromAgents(agents: ReadonlyArray<{ id: string }> | null | undefined): Backend[] {
  const seen = new Set<Backend>();
  for (const agent of agents ?? []) {
    const backend = AGENT_ID_TO_BACKEND[agent.id];
    if (backend) seen.add(backend);
  }
  return BACKEND_ORDER.filter((backend) => seen.has(backend));
}
