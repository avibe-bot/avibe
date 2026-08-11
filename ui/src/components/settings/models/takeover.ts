import { classifyChainLink } from './sourceStateClassification';
import type { AgentChain, AgentChainLink } from './types';

export const currentChainLink = (chain: AgentChain): AgentChainLink | null => {
  if (!chain.current) return null;
  return chain.chain.find((link) => (
    link.source_id === chain.current?.source_id
    && link.model_id === chain.current.model_id
  )) ?? null;
};

export const isTakeoverChain = (chain: AgentChain): boolean => {
  const current = currentChainLink(chain);
  const head = chain.chain[0];
  if (!current || !head) return false;
  const advanced = current.source_id !== head.source_id || current.model_id !== head.model_id;
  return advanced && !head.runnable && classifyChainLink(head) === 'self_healing';
};
