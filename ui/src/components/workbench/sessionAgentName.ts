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

// Which backend a chat session's replies come from, for their avatar: the session's
// own backend, else its Agent's, else (while it inherits the global default) the
// default Agent's. A caller passes no default for a row that never inherits one.
export function sessionAgentBackend(
  session: Pick<WorkbenchSession, 'agent_id' | 'agent_name' | 'agent_backend'>,
  agents: VibeAgentBrief[],
  defaultAgentName: string | null,
): string | null {
  const backend = session.agent_backend?.trim();
  if (backend) return backend;
  const agentName = session.agent_name?.trim() || null;
  const agent = agentName
    ? (session.agent_id ? agents.find((candidate) => candidate.id === session.agent_id) : undefined)
      ?? agents.find((candidate) => candidate.name === agentName)
    : defaultAgentName ? agents.find((candidate) => candidate.name === defaultAgentName) : undefined;
  return agent?.backend ?? null;
}
