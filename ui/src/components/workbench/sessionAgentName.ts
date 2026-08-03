// How a chat session names its Agent. The Agent row is matched by id first and by
// name only as a fallback, so a session whose Agent was renamed still reads back
// under the current display name while a stale catalog entry cannot claim it.
// Pure so the precedence is asserted without mounting the page
// (see ChatArchivedReadOnly.test.tsx).
import type { VibeAgentBrief, WorkbenchSession } from '../../context/ApiContext';

export function sessionAgentDisplayName(
  session: Pick<WorkbenchSession, 'agent_id' | 'agent_name'>,
  agents: VibeAgentBrief[],
): string | null {
  const agentName = session.agent_name?.trim() || null;
  if (!agentName) return null;
  const agent =
    (session.agent_id ? agents.find((candidate) => candidate.id === session.agent_id) : undefined) ??
    agents.find((candidate) => candidate.name === agentName);
  return agent?.display_name?.trim() || agentName;
}
