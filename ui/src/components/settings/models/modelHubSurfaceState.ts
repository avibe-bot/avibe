import type { AgentSupply } from './types';
import { getBackendUiMeta } from '@/lib/agentBackends';

/** Match runtime_stop: the upgrade-added empty Avibe catalog holds no runtime. */
export const agentUsesHubRuntime = (agent: AgentSupply): boolean =>
  agent.mode === 'hub' && (getBackendUiMeta(agent.backend).capabilities.supports_cli || (agent.catalog_models?.length ?? 0) > 0);
