// How a chat session names its Agent. The Agent row is matched by id first and by
// name only as a fallback, so a session whose Agent was renamed still reads back
// under the current display name while a stale catalog entry cannot claim it.
// Pure so the precedence is asserted without mounting the page
// (see ChatArchivedReadOnly.test.tsx).
import type { VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';

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
// that wrote them. A reply records that Agent's id and backend (``author_agent_id``,
// ``author_backend`` in its metadata); the name comes from the catalog by that id, else
// the name recorded with the reply, so a reused name or an empty catalog (a read-only
// viewer) cannot misattribute it. A reply that records neither, or the session's own
// Agent and backend, is the session's. ``null`` means the session's Agent.
export function messageAuthorAgent(
  message: Pick<WorkbenchMessage, 'author_name' | 'metadata'>,
  session: Pick<WorkbenchSession, 'agent_id'>,
  sessionBackend: string | null,
  agents: VibeAgentBrief[],
): { displayName: string; backend: string | null } | null {
  const metadata = (message.metadata ?? {}) as Record<string, unknown>;
  const text = (value: unknown) => (typeof value === 'string' ? value.trim() : '');
  const agentId = text(metadata.author_agent_id);
  const backend = text(metadata.author_backend);
  const otherAgent = Boolean(agentId && session.agent_id && agentId !== session.agent_id);
  const otherBackend = Boolean(backend && sessionBackend && backend !== sessionBackend);
  if (!otherAgent && !otherBackend) return null;
  const agent = agentId ? agents.find((candidate) => candidate.id === agentId) : undefined;
  return {
    displayName: agent?.display_name?.trim() || text(message.author_name) || agent?.name || backend,
    backend: backend || agent?.backend || null,
  };
}
