import { useEffect, useMemo, useState } from 'react';

import { useApi, type RunningAgentsResult, type WorkbenchEventHandlers } from '@/context/ApiContext';
import { onPageReactivated } from '@/lib/pageActivity';

import { FencedSource } from './fencedSource';
import { conversationAgentWorking } from './petState';

// Turn and status events cover Workbench sessions. This slower read catches
// IM turns and agents that stop without an event.
const RECONCILE_INTERVAL_MS = 30 * 1000;
// `turn.start` is published before the backend registers the agent as active
// (a cold Claude or Codex turn first creates its client), so a started
// session is re-read at this pace until the snapshot shows it, its turn ends,
// or the cap passes.
const REGISTRATION_RETRY_MS = 2 * 1000;
const REGISTRATION_CAP_MS = 30 * 1000;
const ALL = 'all';

/**
 * Every event that can change an input of `conversationAgentWorking` and is
 * answered by a plain re-read: an agent's state (`session.status`,
 * `runs.updated`), a session's visibility or existence (`session.activity`),
 * which agents this reader may see (authorization), and an event gap
 * (reconnect). `turn.start` and `turn.end` also re-read, through the
 * registration wait below.
 */
export const REFRESH_TRIGGERS = [
  'onConnected',
  'onSessionStatus',
  'onRunsUpdated',
  'onSessionActivity',
  'onAuthorizationChanged',
] as const satisfies readonly (keyof WorkbenchEventHandlers)[];

/** Sessions with an active agent in the snapshot, whatever their visibility. */
const activeSessions = (result: RunningAgentsResult): Set<string> =>
  new Set(
    result.ok
      ? result.agents.flatMap((agent) => (agent.state === 'active' && agent.session_id ? [agent.session_id] : []))
      : [],
  );

/**
 * The running-agents snapshot for the pet, fenced and coalesced, plus the
 * started turns it is still waiting to see registered.
 */
class ConversationWorkingSource {
  private readonly fenced: FencedSource<typeof ALL, RunningAgentsResult>;
  /** Started sessions not yet seen active, with when to stop waiting. */
  private readonly awaiting = new Map<string, number>();
  private retry: number | undefined;

  constructor(read: () => Promise<RunningAgentsResult>, onWorking: (working: boolean) => void) {
    this.fenced = new FencedSource({
      read,
      apply: (_key, result) => {
        onWorking(conversationAgentWorking(result));
        this.settle(activeSessions(result));
      },
      fail: () => onWorking(false),
    });
  }

  /** Read while enabled; disabled, drop whatever is in flight or waiting. */
  setEnabled(enabled: boolean): void {
    this.fenced.setKey(enabled ? ALL : null);
    if (enabled) return;
    this.awaiting.clear();
    window.clearTimeout(this.retry);
  }

  refresh(): void {
    this.fenced.refresh();
  }

  turnStarted(sessionId: string): void {
    this.awaiting.set(sessionId, Date.now() + REGISTRATION_CAP_MS);
    this.refresh();
  }

  turnEnded(sessionId: string): void {
    this.awaiting.delete(sessionId);
    this.refresh();
  }

  private settle(active: Set<string>): void {
    const now = Date.now();
    for (const [sessionId, until] of this.awaiting) {
      if (active.has(sessionId) || until <= now) this.awaiting.delete(sessionId);
    }
    window.clearTimeout(this.retry);
    if (this.awaiting.size > 0) {
      this.retry = window.setTimeout(() => this.refresh(), REGISTRATION_RETRY_MS);
    }
  }
}

/**
 * Whether an agent is working in any conversation the user can see, from the
 * running-agents snapshot the Agents page reads. `enabled` is the caller's
 * permission to read it; without it the answer is always false.
 */
export function useConversationAgentWorking(enabled: boolean): boolean {
  const api = useApi();
  const [working, setWorking] = useState(false);
  const source = useMemo(() => new ConversationWorkingSource(() => api.getRunningAgents(), setWorking), [api]);

  useEffect(() => {
    // Disabled, the return value below ignores whatever was last read.
    source.setEnabled(enabled);
    if (!enabled) return undefined;
    const refresh = () => source.refresh();
    refresh();
    const handlers: WorkbenchEventHandlers = {
      onTurnStart: ({ session_id }) => source.turnStarted(session_id),
      onTurnEnd: ({ session_id }) => source.turnEnded(session_id),
    };
    for (const trigger of REFRESH_TRIGGERS) handlers[trigger] = refresh;
    const disconnect = api.connectWorkbenchEvents(handlers);
    const stopReactivation = onPageReactivated(refresh);
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, RECONCILE_INTERVAL_MS);
    return () => {
      disconnect();
      stopReactivation();
      window.clearInterval(interval);
      source.setEnabled(false);
    };
  }, [api, enabled, source]);

  return enabled && working;
}
