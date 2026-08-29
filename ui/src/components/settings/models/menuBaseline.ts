import type { CollectionReadAuthority } from './collectionReadAuthority';
import type { ModelsApi } from './modelsApi';
import { combineSourceOrderReads } from './sourceOrderComposition';
import type { AgentMenu, AgentSupply, Source } from './types';

export type OpenCodeMenuBaseline = {
  agent: AgentSupply;
  sources: Source[];
};

export type OpenCodeMenuIntent = {
  added: string[];
  removed: Set<string>;
};

const sameSourceSnapshot = (left: Source[], right: Source[]): boolean =>
  JSON.stringify(left) === JSON.stringify(right);

/** The Agent read is authoritative only when the complete Source projection is
 * unchanged on both sides of it. Source ids alone cannot detect model edits. */
export const readOpenCodeMenuBaseline = async (
  api: Pick<ModelsApi, 'getAgentSources'>,
  sourceReads: Pick<CollectionReadAuthority<Source[]>, 'readValue'>,
): Promise<OpenCodeMenuBaseline> => {
  const before = await sourceReads.readValue();
  const agent = await api.getAgentSources('opencode');
  const after = await sourceReads.readValue();
  if (agent.backend !== 'opencode' || agent.mode !== 'hub' || agent.menu_kind !== 'open') {
    throw new Error('OpenCode Model Hub menu is unavailable');
  }
  if (!sameSourceSnapshot(before, after) || combineSourceOrderReads(agent, after).hasHole) {
    throw new Error('OpenCode Model Hub menu reads did not converge');
  }
  return { agent, sources: after };
};

export const openCodeMenuIntent = (
  baseline: readonly string[],
  draft: readonly string[],
): OpenCodeMenuIntent => {
  const baselineSet = new Set(baseline);
  const draftSet = new Set(draft);
  return {
    added: draft.filter((id) => !baselineSet.has(id)),
    removed: new Set(baseline.filter((id) => !draftSet.has(id))),
  };
};

/** Rebase only explicit user edits; unrelated changes in a newer menu survive. */
export const applyOpenCodeMenuIntent = (
  current: readonly string[],
  intent: OpenCodeMenuIntent,
  selectableIds: ReadonlySet<string>,
): string[] => {
  const rebased = current.filter((id) => !intent.removed.has(id));
  const present = new Set(rebased);
  for (const id of intent.added) {
    if (!selectableIds.has(id) || present.has(id)) continue;
    rebased.push(id);
    present.add(id);
  }
  return rebased;
};

export const sameOpenCodeMenu = (left: AgentMenu | null | undefined, right: AgentMenu): boolean => {
  if (!left || left.view !== right.view || left.checked.length !== right.checked.length) return false;
  return left.checked.every((id, index) => id === right.checked[index]);
};
