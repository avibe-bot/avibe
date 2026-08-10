// Remembers which view the Agents surface was left on, so re-entering it from
// the sidebar resumes where the user was instead of always reopening
// Definitions. Two independent memories because they are nested: the page tab
// (Definitions / Runs) and, inside Runs, the run-graph 活跃/含历史 mode.
//
// Storage conventions mirror chatViewMemory: versioned `avibe.*` key, injectable
// storage for tests, best-effort try/catch so private mode / blocked storage
// degrades to the defaults rather than throwing.
export type AgentsTabKey = 'definitions' | 'running';
export type AgentGraphMode = 'active' | 'history';

export const AGENTS_TAB_ORDER: AgentsTabKey[] = ['definitions', 'running'];
export const DEFAULT_AGENTS_TAB: AgentsTabKey = 'definitions';
export const DEFAULT_AGENT_GRAPH_MODE: AgentGraphMode = 'history';

const TAB_STORAGE_KEY = 'avibe.agents.tab.v1';
const GRAPH_MODE_STORAGE_KEY = 'avibe.agents.graph-mode.v1';

type ViewStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(storage?: ViewStorage): ViewStorage | undefined {
  return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
}

function read<T extends string>(key: string, allowed: readonly T[], fallback: T, storage?: ViewStorage): T {
  try {
    const raw = browserStorage(storage)?.getItem(key);
    // An unknown value (older build, hand-edited storage, removed tab) opens the
    // default rather than selecting nothing.
    return (allowed as readonly string[]).includes(raw ?? '') ? (raw as T) : fallback;
  } catch {
    return fallback;
  }
}

function write(key: string, value: string, storage?: ViewStorage): void {
  try {
    browserStorage(storage)?.setItem(key, value);
  } catch {
    // View memory is best-effort in private browsing and restricted storage contexts.
  }
}

export function readAgentsTab(storage?: ViewStorage): AgentsTabKey {
  return read(TAB_STORAGE_KEY, AGENTS_TAB_ORDER, DEFAULT_AGENTS_TAB, storage);
}

// A contextual link that needs a specific tab says so with ``?tab=`` — e.g.
// "New agent" / "Open in Agents" both need the Definitions controls, not the run
// graph, whichever tab the user happens to have left the page on. Unlike
// harnessTabFromParam this returns null rather than the default for an absent or
// unknown value, because here "no explicit intent" means *resume the remembered
// tab*, which only the caller can fall back to.
export function agentsTabFromParam(param: string | null | undefined): AgentsTabKey | null {
  return (AGENTS_TAB_ORDER as string[]).includes(param ?? '') ? (param as AgentsTabKey) : null;
}

export function writeAgentsTab(tab: AgentsTabKey, storage?: ViewStorage): void {
  write(TAB_STORAGE_KEY, tab, storage);
}

export function readAgentGraphMode(storage?: ViewStorage): AgentGraphMode {
  return read(
    GRAPH_MODE_STORAGE_KEY,
    ['active', 'history'] as const,
    DEFAULT_AGENT_GRAPH_MODE,
    storage,
  );
}

export function writeAgentGraphMode(mode: AgentGraphMode, storage?: ViewStorage): void {
  write(GRAPH_MODE_STORAGE_KEY, mode, storage);
}
