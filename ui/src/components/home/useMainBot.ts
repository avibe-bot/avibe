import { useCallback, useEffect, useState } from 'react';

import { useApi, type SessionSpeed, type VibeAgentBrief, type WorkbenchSession, type WorkbenchSessionUpdate } from '../../context/ApiContext';
import { isBackend } from '../../lib/backendAccent';
import { MAIN_BOT_SESSION_ID } from '../../lib/bots';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { useCoalescedWrite } from '../../lib/useCoalescedWrite';
import type { AgentRoutePatch } from '../workbench/AgentRoutePicker';
import {
  bySessionWriteGroup,
  commitSessionRowWrite,
  createSessionRowRefreshGate,
  mergeSessionPatchWrites,
  recordSessionRowWrite,
  releaseSessionRowWrite,
  sessionPatchWriteStandsAlone,
  useChatSessionRow,
  type SessionPatchWrite,
  type SessionWriteGroup,
} from '../workbench/sessionRowRefresh';

/** What the composer's picker changes: the model and effort (the Agent is the Bot's), or the speed. */
export type MainBotRouteChange = AgentRoutePatch | { speed: SessionSpeed };

type Write = SessionPatchWrite<WorkbenchSession>;

export interface MainBotRoute {
  /** The main Bot's conversation as last read (its route: model, effort, speed), with this
   *  document's unanswered writes on top; null until read. */
  session: WorkbenchSession | null;
  /** The main Bot's own Agent; null until read. */
  agent: VibeAgentBrief | null;
  /** A change is being saved. */
  saving: boolean;
  /** Apply a change at once and save it to the main Bot's conversation. */
  change: (patch: MainBotRouteChange) => void;
}

/**
 * The route the home's composer shows: the main Bot's conversation, which is where it sends. Read
 * while the home is on screen, and again when it is uncovered, since Settings or the conversation
 * itself may have moved it.
 *
 * A change goes through the same per-session writers the conversation's own picker uses
 * (`session-route`, `session-meta`, with its payload, merge and refusal rule): one request per
 * Session in flight, the clicks behind it folded into one, and whichever of the two surfaces is on
 * screen serving the burst. So a pick made here cannot race one made in the conversation, even
 * when the conversation opens before this one is answered.
 */
export function useMainBotRoute(enabled: boolean): MainBotRoute {
  const api = useApi();
  const surfaceActive = useRouteSurfaceActive();
  const { session, installFromServer, applyLocal } = useChatSessionRow<WorkbenchSession>();
  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  // Orders this surface's reads of the row against its own writes.
  const [gate] = useState(createSessionRowRefreshGate);

  const read = useCallback(async () => {
    const isCurrent = await gate.begin();
    const [main, list] = await Promise.all([
      // The main Bot's own read, which creates the Bot and its conversation on first use: on a
      // fresh instance the home can ask before anything else has.
      api.getMainSession().catch(() => null),
      api.listVibeAgents({ includeDisabled: false }).catch(() => null),
    ]);
    const row = main?.session ?? null;
    if (row) installFromServer((prev) => (isCurrent() ? row : prev));
    if (list) setAgents(list.agents);
  }, [api, gate, installFromServer]);

  const readable = enabled && surfaceActive;
  useEffect(() => {
    if (readable) queueMicrotask(() => void read());
  }, [readable, read]);

  const send = useCallback(async ({ changes, gate: writeGate, group }: Write, sessionId: string) => {
    const finish = writeGate.beginMutation();
    try {
      await api.updateSession(sessionId, changes as Partial<WorkbenchSessionUpdate>);
      commitSessionRowWrite<WorkbenchSession>(sessionId, changes, group);
      return true;
    } catch {
      return false;
    } finally {
      finish();
    }
  }, [api]);
  // Once per burst: a committed one is read back; a refused one puts back what it replaced, then
  // reads, since only a read can show a field someone else moved.
  const settle = useCallback((sessionId: string, committed: boolean, group: SessionWriteGroup) => {
    const base = releaseSessionRowWrite<WorkbenchSession>(sessionId, group);
    if (sessionId !== MAIN_BOT_SESSION_ID) return;
    if (committed) return read();
    if (base) {
      gate.invalidate();
      applyLocal((prev) => (prev && prev.id === sessionId ? { ...prev, ...base } : prev));
    }
    void read();
  }, [applyLocal, gate, read]);
  const routeWriter = useCoalescedWrite<Write>('session-route', send, {
    merge: mergeSessionPatchWrites<WorkbenchSession>,
    standsAlone: sessionPatchWriteStandsAlone<WorkbenchSession>,
    onSettled: useCallback((sessionId: string, committed: boolean) => settle(sessionId, committed, 'route'), [settle]),
  });
  const metaWriter = useCoalescedWrite<Write>('session-meta', send, {
    merge: mergeSessionPatchWrites<WorkbenchSession>,
    standsAlone: sessionPatchWriteStandsAlone<WorkbenchSession>,
    onSettled: useCallback((sessionId: string, committed: boolean) => settle(sessionId, committed, 'meta'), [settle]),
  });

  const change = useCallback((patch: MainBotRouteChange) => {
    if (!session) return;
    const changes = patch as Partial<WorkbenchSession>;
    // A row read already in flight must not re-install the route this pick replaces.
    gate.invalidate();
    applyLocal((prev) => (prev && prev.id === session.id ? { ...prev, ...changes } : prev));
    for (const [group, groupChanges] of bySessionWriteGroup(changes)) {
      const writer = group === 'route' ? routeWriter : metaWriter;
      const opened = writer.write(session.id, { changes: groupChanges, gate, group });
      recordSessionRowWrite(session, groupChanges, opened, group);
    }
    if ('speed' in patch) {
      // The latest choice is where the next new Session starts, as in the conversation's picker.
      void api.setFastModeDefault(patch.speed === 'fast').catch(() => undefined);
    }
  }, [api, applyLocal, gate, metaWriter, routeWriter, session]);

  const agent = session?.agent_name ? agents.find((each) => each.name === session.agent_name) ?? null : null;
  const saving = routeWriter.isSaving(MAIN_BOT_SESSION_ID) || metaWriter.isSaving(MAIN_BOT_SESSION_ID);
  return { session, agent, saving, change };
}

/**
 * Whether the main Bot has no model to run on, as its backend reports it: the read answered for
 * that backend, and no credential supplies it (`auth: "none"`) or the backend is off. Anything not
 * observed (not asked yet, a failed or refused read, an answer about another backend) is not "no
 * model". Read while the home is on screen, again when it is uncovered (the way back from
 * Settings · Models), and when the window regains focus.
 */
export function useNoModel(backend: string | null, asked: boolean): boolean {
  const api = useApi();
  const surfaceActive = useRouteSurfaceActive();
  const name = asked && surfaceActive && backend && isBackend(backend) ? backend : null;
  const [observed, setObserved] = useState<{ backend: string; none: boolean } | null>(null);
  useEffect(() => {
    if (!name) return;
    let current = true;
    const read = () => {
      void api.getBackendConnection(name)
        .then((state) => {
          if (!current || state?.ok !== true || state.backend !== name) return;
          setObserved({ backend: name, none: state.auth === 'none' || state.enabled === false });
        })
        .catch(() => {});
    };
    read();
    window.addEventListener('focus', read);
    return () => {
      current = false;
      window.removeEventListener('focus', read);
    };
  }, [api, name]);
  return observed !== null && observed.backend === backend && observed.none;
}
