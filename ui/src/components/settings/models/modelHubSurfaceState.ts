import type { AgentSupply, Source } from './types';

export type ModelsSurfaceKind = 'direct_empty' | 'gateway';

export const modelsSurfaceKind = (agents: AgentSupply[], sources: Source[]): ModelsSurfaceKind =>
  agents.every((agent) => agent.mode === 'direct') && sources.length === 0
    ? 'direct_empty'
    : 'gateway';
