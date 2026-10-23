// How a chat session names its Agent. The Agent row is matched by id first and by
// name only as a fallback, so a session whose Agent was renamed still reads back
// under the current display name while a stale catalog entry cannot claim it.
// Pure so the precedence is asserted without mounting the page
// (see ChatArchivedReadOnly.test.tsx).
import type { VibeAgentBrief, WorkbenchSession } from '../../context/ApiContext';
import { BACKEND_CATALOG } from '../../lib/agentBackendCatalog.generated';

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

// Which Agent wrote one reply, when that is not the session's Agent: a session switched
// to another Agent (say from Codex to Vibey) keeps its earlier replies under the Agent
// that wrote them. ``authorName`` is the Agent name recorded with the reply. A name no
// Agent carries any more (renamed or archived) reads as the session's own Agent, as
// before, unless it names a built-in backend Agent, whose name never changes.
// ``null`` means the session's Agent.
export function messageAuthorAgent(
  authorName: string | null | undefined,
  session: Pick<WorkbenchSession, 'agent_id' | 'agent_name'>,
  agents: VibeAgentBrief[],
): { displayName: string; backend: string } | null {
  const name = authorName?.trim();
  if (!name || name === session.agent_name?.trim()) return null;
  const agent = agents.find((candidate) => candidate.name === name);
  if (agent) {
    return agent.id === session.agent_id
      ? null
      : { displayName: agent.display_name?.trim() || agent.name, backend: agent.backend };
  }
  return BACKEND_CATALOG.some((backend) => backend.id === name) ? { displayName: name, backend: name } : null;
}
