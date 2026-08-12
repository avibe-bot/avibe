import { classifyChainLink } from './sourceStateClassification';
import { equalHopIdentity } from './hopIdentity';
import type { AgentChain, AgentChainLink } from './types';

export const currentChainLink = (chain: AgentChain): AgentChainLink | null => {
  if (!chain.current) return null;
  return chain.chain.find((link) => equalHopIdentity(link, chain.current)) ?? null;
};

export const isTakeoverChain = (chain: AgentChain): boolean => {
  const current = currentChainLink(chain);
  const head = chain.chain[0];
  if (!current || !head) return false;
  const advanced = !equalHopIdentity(current, head);
  return advanced && !head.runnable && classifyChainLink(head) === 'self_healing';
};
