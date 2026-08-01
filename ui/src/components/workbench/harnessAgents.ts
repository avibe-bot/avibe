import type { ApiContextType, VibeAgentBrief } from '../../context/ApiContext';

export async function loadHarnessAgentCatalog(
  api: Pick<ApiContextType, 'listVibeAgents'>,
): Promise<Record<string, VibeAgentBrief>> {
  const result = await api.listVibeAgents({
    includeDisabled: true,
    includeArchived: true,
    cache: false,
  });
  return Object.fromEntries(result.agents.map((agent) => [agent.name, agent]));
}
