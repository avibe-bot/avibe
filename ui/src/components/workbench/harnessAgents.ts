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

/** How a run names its Agent: the Agent's own display name, else the raw name it
 *  was dispatched under, else an em dash. An Agent renamed or archived after the
 *  run still reads back under the name the run actually used. */
export function agentDisplayName(
  agentName: string | null | undefined,
  agent?: Pick<VibeAgentBrief, 'display_name'>,
): string {
  return agent?.display_name || agentName || '—';
}
