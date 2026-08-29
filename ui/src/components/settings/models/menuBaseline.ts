import type { RegionRead } from './regionRead';
import type { AgentSupply, Source } from './types';

/** A whole-menu write is safe only when both server-owned halves of its baseline are current. */
export const openCodeMenuBaselineReady = (
  supply: RegionRead<AgentSupply[]>,
  sources: RegionRead<Source[]>,
): boolean => supply.kind === 'ready' && sources.kind === 'ready';
