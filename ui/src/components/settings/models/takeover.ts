import type { AgentChain, AgentChainLink, ChainHealth, ChainUnavailableReason } from './types';

const SELF_HEALING_HEAD: Readonly<Record<ChainHealth, boolean>> = {
  healthy: false,
  cooldown: true,
  needs_action: false,
  error: false,
};

const RECOVERABLE_PROCESS_REASON: Readonly<Record<ChainUnavailableReason, boolean>> = {
  native_cli_unavailable: false,
  source_missing: false,
  model_unsupported: false,
};

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
  const processCanRecover = head.reason === null || RECOVERABLE_PROCESS_REASON[head.reason];
  return advanced && !head.runnable && SELF_HEALING_HEAD[head.health] && processCanRecover;
};
