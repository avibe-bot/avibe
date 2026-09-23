import type { VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import { ASSISTANT_ORDER, type AssistantId } from './collaborationTimeline';

/**
 * Which Agent a setup card speaks for.
 *
 * Setup shows one card per assistant, not one card per Agent: the route editor
 * edits a backend's route, and the entry gate prefers that same Agent when it
 * has to pick a default. Both need the *same* answer to "which Agent is this
 * card", so the answer lives here once rather than being re-derived on each
 * screen.
 *
 * D4/D9 defines it as the store's builtin selection. The brief list does not
 * carry that mark — `source` is provenance, not identity — so a custom Agent
 * named `claude` must not take the card. The producer is
 * `is_builtin_default_agent`: metadata `builtin_default` or `lock_delete`.
 * Only an uncached full Agent read has those fields. A missing or failed read
 * is not a designated target; it is an explicit existing-name choice later,
 * not an inferred one now.
 */
const DEFAULT_AGENT_NAME = 'default';

export type SetupTargetAgent = Pick<VibeAgentBrief, 'name' | 'backend' | 'enabled' | 'archived'> & {
  metadata?: Record<string, unknown>;
};

export const isBuiltinAgent = (agent: { metadata?: Record<string, unknown> }): boolean =>
  Boolean(agent.metadata?.builtin_default || agent.metadata?.lock_delete);

/** Deterministic everywhere: code-unit order, not a locale's collation. */
const byName = (left: { name: string }, right: { name: string }): number =>
  left.name < right.name ? -1 : left.name > right.name ? 1 : 0;

export function setupTargetFor<T extends SetupTargetAgent>(
  agents: readonly T[],
  backend: AssistantId,
): T | null {
  const pool = agents.filter((agent) =>
    agent.backend === backend && agent.enabled && !agent.archived && isBuiltinAgent(agent));
  return pool.find((agent) => agent.name === backend)
    ?? pool.find((agent) => agent.name === DEFAULT_AGENT_NAME)
    ?? [...pool].sort(byName)[0]
    ?? null;
}

/** Every assistant's own Agent, in C6's stable order, skipping the ones that have none. */
export function setupTargets<T extends SetupTargetAgent>(agents: readonly T[]): T[] {
  return ASSISTANT_ORDER.flatMap((backend) => {
    const target = setupTargetFor(agents, backend);
    return target ? [target] : [];
  });
}

export type SetupTargetReader = {
  getVibeAgent: (
    name: string,
    params?: { cache?: boolean },
  ) => Promise<{ ok: boolean; agent?: VibeAgentFull | null }>;
};

const sameCurrentIdentity = (brief: VibeAgentBrief, full: VibeAgentFull): boolean =>
  brief.name === full.name && brief.backend === full.backend;

/**
 * Designated setup targets from the store's current records, not from briefs.
 *
 * Relevant enabled/non-archived backend candidates are queried concurrently
 * with `{cache:false}`. The object handed back is the full current record so
 * name, backend and model stay the ones the store just answered. A failed,
 * unread, identity-mismatched or unmarked record does not become a target.
 */
export async function readSetupTargets(
  briefs: readonly VibeAgentBrief[],
  reader: SetupTargetReader,
): Promise<VibeAgentFull[]> {
  const seen = new Set<string>();
  const relevant = briefs.filter((agent) => {
    if (!agent.enabled || agent.archived) return false;
    if (!ASSISTANT_ORDER.includes(agent.backend as AssistantId)) return false;
    if (seen.has(agent.name)) return false;
    seen.add(agent.name);
    return true;
  });
  const records = await Promise.all(relevant.map(async (brief) => {
    try {
      const result = await reader.getVibeAgent(brief.name, { cache: false });
      if (!result.ok || result.agent == null) return null;
      if (!sameCurrentIdentity(brief, result.agent)) return null;
      if (!result.agent.enabled || result.agent.archived) return null;
      return result.agent;
    } catch {
      return null;
    }
  }));
  return setupTargets(records.filter((record): record is VibeAgentFull => record !== null));
}
