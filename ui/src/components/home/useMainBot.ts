import { useCallback, useEffect, useRef, useState } from 'react';

import { useApi, type SessionSpeed, type VibeAgentBrief, type WorkbenchSession, type WorkbenchSessionUpdate } from '../../context/ApiContext';
import type { AgentRoutePatch } from '../workbench/AgentRoutePicker';
import { isBackend } from '../../lib/backendAccent';
import { MAIN_BOT_SESSION_ID } from '../../lib/bots';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';

/** What the composer's picker changes: the model and effort (the Agent is the Bot's), or the speed. */
export type MainBotRouteChange = AgentRoutePatch | { speed: SessionSpeed };

export interface MainBotRoute {
  /** The main Bot's conversation as last read (its route: model, effort, speed); null until read. */
  session: WorkbenchSession | null;
  /** The main Bot's own Agent; null until read. */
  agent: VibeAgentBrief | null;
  /** A change is being saved. */
  saving: boolean;
  /** Apply a change at once and save it to the main Bot's conversation, the same write its own
   *  picker makes; once the last of a burst settles, the conversation is read back. */
  change: (patch: MainBotRouteChange) => void;
}

/**
 * The route the home's composer shows: the main Bot's conversation, which is where it sends. Read
 * while the home is on screen, and again when it is uncovered, since Settings or the conversation
 * itself may have moved it.
 */
export function useMainBotRoute(enabled: boolean): MainBotRoute {
  const api = useApi();
  const surfaceActive = useRouteSurfaceActive();
  const [session, setSession] = useState<WorkbenchSession | null>(null);
  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  const [saving, setSaving] = useState(false);
  // Every write fences the reads started before it, so a slow read cannot put the old route back.
  const generationRef = useRef(0);
  const pendingRef = useRef(0);

  const read = useCallback(async () => {
    const generation = ++generationRef.current;
    const [row, list] = await Promise.all([
      api.getSession(MAIN_BOT_SESSION_ID, { cache: false, handleError: false }).catch(() => null),
      api.listVibeAgents({ includeDisabled: false }).catch(() => null),
    ]);
    if (generation !== generationRef.current) return;
    if (row) setSession(row);
    if (list) setAgents(list.agents);
  }, [api]);

  const readable = enabled && surfaceActive;
  useEffect(() => {
    if (readable) queueMicrotask(() => void read());
  }, [readable, read]);

  const change = useCallback((patch: MainBotRouteChange) => {
    generationRef.current += 1;
    pendingRef.current += 1;
    setSaving(true);
    setSession((current) => (current ? { ...current, ...patch } as WorkbenchSession : current));
    if ('speed' in patch) {
      // The latest choice is where the next new Session starts, as in the conversation's picker.
      void api.setFastModeDefault(patch.speed === 'fast').catch(() => undefined);
    }
    void api.updateSession(MAIN_BOT_SESSION_ID, patch as Partial<WorkbenchSessionUpdate>)
      .catch(() => undefined)
      .finally(() => {
        pendingRef.current -= 1;
        if (pendingRef.current > 0) return;
        setSaving(false);
        void read();
      });
  }, [api, read]);

  const agent = session?.agent_name ? agents.find((each) => each.name === session.agent_name) ?? null : null;
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
