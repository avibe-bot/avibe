import { useCallback, useEffect, useMemo, useState } from 'react';

import { useApi } from '../context/ApiContext';
import { useWorkbenchProjectsTree } from '../context/WorkbenchProjectsContext';
import type { ApiContextType, VibeAgentBrief, WorkbenchProject, WorkbenchSessionCreate } from '../context/ApiContext';

interface UseNewSessionOptions {
  /** Re-run the per-open reset on the rising edge — sheets pass their `open`. Default true. */
  active?: boolean;
  /** Pre-translated copy: the hook stays i18n-free, callers pass t(...) strings. */
  loadErrorText: string;
  createFailedText: string;
}

// The agent/model/effort selection (agent route). Empty = the server default
// Agent. Fields allow null because the AgentRoutePicker emits null to clear
// model/effort when switching agents; send() drops nulls before creating so
// the create payload only carries real values.
export interface AgentRouteSelection {
  agent_backend?: string | null;
  agent_name?: string | null;
  agent_id?: string | null;
  agent_variant?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
}

export interface NewSessionState {
  projects: WorkbenchProject[];
  loaded: boolean;
  error: string | null;
  sending: boolean;
  selectedId: string | null;
  setSelected: (id: string) => void;
  target: WorkbenchProject | null;
  needsProject: boolean;
  // The EFFECTIVE agent route (agent + model + effort): the user's pick, else
  // the selected project's default, else empty (= global default).
  agents: VibeAgentBrief[];
  defaultAgentName: string | null;
  /** Name for the picker's "Default" option: the project default's agent when set, else global. */
  effectiveDefaultAgentName: string | null;
  agentRoute: AgentRouteSelection;
  setAgentRoute: (patch: AgentRouteSelection) => void;
  /** Creates a session under `target` (with the picked agent route, if any) and returns the
   *  nav target; null if it couldn't start. The hook never navigates — the caller does. */
  send: (text: string) => Promise<{ sessionId: string; initialMessage: string } | null>;
  upsertSelectProject: (project: WorkbenchProject) => void;
}

const normalizeAgentName = (name: string) =>
  name.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '');

export function isProjectDefaultAgentAvailable(
  project: WorkbenchProject | null,
  agents: VibeAgentBrief[],
): boolean {
  const name = project?.default_agent?.agent_name;
  if (!name) return false;
  const normalizedName = normalizeAgentName(name);
  return agents.some(
    (agent) => normalizeAgentName(agent.name) === normalizedName && agent.enabled && !agent.archived,
  );
}

export function projectDefaultAgentRoute(
  project: WorkbenchProject | null,
  agents: VibeAgentBrief[],
): AgentRouteSelection {
  if (!isProjectDefaultAgentAvailable(project, agents)) return {};
  const def = project?.default_agent;
  return def
    ? {
        agent_name: def.agent_name,
        agent_id: def.agent_id,
        agent_variant: def.agent_variant,
        model: def.model,
        reasoning_effort: def.reasoning_effort,
      }
    : {};
}

const sortByRecent = (list: WorkbenchProject[]) =>
  list
    .slice()
    .sort((a, b) => (b.last_active_at || b.created_at).localeCompare(a.last_active_at || a.created_at));

type AgentProjectionResponse = Awaited<ReturnType<ApiContextType['listVibeAgents']>>;

export function createLatestAgentProjectionLoader(
  fetchAgents: () => Promise<AgentProjectionResponse>,
  applyProjection: (response: AgentProjectionResponse | null) => void,
  setProjectionLoaded: (loaded: boolean) => void,
) {
  let generation = 0;
  let stopped = false;

  return {
    load: async () => {
      const loadGeneration = ++generation;
      setProjectionLoaded(false);
      try {
        const response = await fetchAgents();
        if (!stopped && loadGeneration === generation) {
          applyProjection(response);
          setProjectionLoaded(true);
        }
      } catch {
        if (!stopped && loadGeneration === generation) {
          applyProjection(null);
          setProjectionLoaded(true);
        }
      }
    },
    stop: () => {
      stopped = true;
      generation += 1;
    },
  };
}

// Shared new-session create flow for the desktop Workbench home (`Workbench.tsx`)
// and the mobile NewSessionSheet. A thin layer over the shared projects provider
// (the project LIST + the create itself come from there, so a project/session
// created here shows up in the sidebar + Projects tree). It adds the picker
// selections (project + agent route), the transient sending/error state, and
// target resolution. Navigation + draft + the sheet's open/close lifecycle stay
// in the consumer.
export function useNewSession({ active = true, loadErrorText, createFailedText }: UseNewSessionOptions): NewSessionState {
  const api = useApi();
  // `active` already gates the agent projection below; pass it to the tree too.
  // NewSessionSheet is mounted shell-wide, so an unconditional read would make
  // the tree bootstrap on every route again — exactly what activation prevents.
  const {
    projects: rawProjects,
    projectsError,
    createSessionForProject,
    upsertProjectToTop,
  } = useWorkbenchProjectsTree({ active });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  const [defaultAgentName, setDefaultAgentName] = useState<string | null>(null);
  const [agentProjectionLoaded, setAgentProjectionLoaded] = useState(false);
  // The user's explicit pick in the composer; {} = "no pick, follow the
  // project/global default". Kept separate from the effective route (below) so
  // the project default can be derived live instead of copied into state.
  const [userPick, setUserPick] = useState<AgentRouteSelection>({});

  const projects = useMemo(
    () => (rawProjects ? sortByRecent(rawProjects.filter((project) => project.capabilities.can_chat)) : []),
    [rawProjects],
  );
  const projectsLoaded = rawProjects !== null;
  const loaded = projectsLoaded && agentProjectionLoaded;

  // Keep the authorized Agent projection live. Authorization changes clear the
  // API cache before this handler runs, so the refresh cannot retain a revoked
  // Agent in an already-open picker.
  useEffect(() => {
    if (!active) return;
    const loader = createLatestAgentProjectionLoader(
      () => api.listVibeAgents({ includeDisabled: false }),
      (res) => {
        if (res) {
          setAgents(res.agents);
          setDefaultAgentName(res.default_agent_name);
          setUserPick((current) => {
            if (!current.agent_name || res.agents.some((agent) => agent.name === current.agent_name)) {
              return current;
            }
            return {};
          });
          return;
        }
        setAgents([]);
        setDefaultAgentName(null);
        setUserPick({});
      },
      setAgentProjectionLoaded,
    );
    void loader.load();
    const disconnect = api.connectWorkbenchEvents({
      onAuthorizationChanged: () => {
        void loader.load();
      },
    });
    return () => {
      loader.stop();
      disconnect();
    };
  }, [active, api]);

  // Clear transient state when the sheet (re)opens so a prior submit / error
  // doesn't leak into the next open. The home passes active=true (runs once).
  useEffect(() => {
    if (!active) return;
    setSending(false);
    setError(null);
  }, [active]);

  // selectedId is the explicit pick; fall back to the most-recent project so a
  // null or now-hidden selection still resolves a sane target.
  const target = projects.find((p) => p.id === selectedId) ?? projects[0] ?? null;
  const needsProject = loaded && !target;

  // Drop a stale pick when the selected project changes, so switching projects
  // falls back to the new project's default (React "adjust state while rendering
  // when a prop changes" pattern, guarded by the previous project id).
  const [pickedProjectId, setPickedProjectId] = useState<string | null>(null);
  if (target && pickedProjectId !== target.id) {
    setPickedProjectId(target.id);
    setUserPick((prev) => (Object.keys(prev).length ? {} : prev));
  }

  // The selected project's default Agent as a route. An archived binding stays
  // on the project for durable history, but the enabled catalog intentionally
  // excludes it, so a new chat falls back to the live global default.
  const projectDefaultAvailable = isProjectDefaultAgentAvailable(target, agents);
  const projectDefaultRoute = useMemo<AgentRouteSelection>(
    () => projectDefaultAgentRoute(target, agents),
    [target, agents],
  );

  // The GLOBAL default Agent resolved to a concrete route, looked up from the
  // agents list by name. The picker needs a concrete route to render the model
  // and to load its model column — an empty route leaves the backend unknown, so
  // the column never fetches and the trigger shows no model. Empty when there is
  // no default agent (or it isn't in the enabled list).
  const globalDefaultRoute = useMemo<AgentRouteSelection>(() => {
    if (!defaultAgentName) return {};
    const def = agents.find((agent) => agent.name === defaultAgentName);
    return def
      ? {
          agent_backend: def.backend,
          agent_name: def.name,
          agent_id: def.id,
          agent_variant: def.backend,
          model: def.model,
          reasoning_effort: def.reasoning_effort,
        }
      : {};
  }, [agents, defaultAgentName]);

  // The route the picker SHOWS when the user hasn't picked: the project default
  // if the project has one, else the resolved global default. Both are concrete
  // so the picker renders the agent + model and loads the model column.
  const hasProjectDefault = Object.keys(projectDefaultRoute).length > 0;
  const fallbackRoute = hasProjectDefault ? projectDefaultRoute : globalDefaultRoute;

  // EFFECTIVE route the picker DISPLAYS: the user's pick if any, else the
  // fallback default — DERIVED, not copied, so editing the project default in
  // Project Settings while this composer stays mounted updates it with no stale
  // state.
  const hasPick = Object.values(userPick).some((value) => value != null && value !== '');
  const agentRoute = hasPick ? userPick : fallbackRoute;

  // Route to POST on create. A user pick or a project default is pinned; the
  // bare global default stays EMPTY so the session is created agent-less and
  // dispatch resolves the live global default (see the agent-less default test
  // in tests/test_workbench_session_defaults.py). Display and create diverge
  // only for the untouched global default — the picker still SHOWS it above.
  const routeForCreate = hasPick ? userPick : projectDefaultRoute;

  // Merge the picker's PARTIAL patches (a lone {model} / {reasoning_effort}) onto
  // the route it is actually showing. Before the first pick userPick is empty, so
  // without this seed a model/effort edit would collapse to an identity-less
  // route and drop the agent. An all-null reset patch still clears back to the
  // fallback default (every field becomes null → hasPick false).
  const applyAgentRoute = useCallback(
    (patch: AgentRouteSelection) =>
      setUserPick((prev) => {
        const hasPrevPick = Object.values(prev).some((value) => value != null && value !== '');
        return { ...(hasPrevPick ? prev : fallbackRoute), ...patch };
      }),
    [fallbackRoute],
  );

  // Label for the picker's "Default" option: the project default's agent when
  // set, otherwise the global default agent.
  const effectiveDefaultAgentName = projectDefaultAvailable
    ? target?.default_agent?.agent_name ?? defaultAgentName
    : defaultAgentName;

  const send = useCallback(
    async (text: string): Promise<{ sessionId: string; initialMessage: string } | null> => {
      const trimmed = text.trim();
      // Never create from a stale/empty/in-flight state; no target → caller opens New Project.
      if (!trimmed || sending || !loaded || !target) return null;
      setSending(true);
      setError(null);
      // routeForCreate pins a user pick or a project default; the bare global
      // default is empty here, so the payload carries only real fields and an
      // empty route sends nothing → the server resolves the global default.
      const overrides: Partial<WorkbenchSessionCreate> = {};
      if (routeForCreate.agent_name) overrides.agent_name = routeForCreate.agent_name;
      if (routeForCreate.agent_id) overrides.agent_id = routeForCreate.agent_id;
      const routeAgent = routeForCreate.agent_name
        ? agents.find((agent) => agent.name === routeForCreate.agent_name) || null
        : null;
      const backendForCreate = routeForCreate.agent_backend || routeAgent?.backend || null;
      if (backendForCreate) overrides.agent_backend = backendForCreate;
      if (routeForCreate.agent_variant || backendForCreate) {
        overrides.agent_variant = routeForCreate.agent_variant || backendForCreate || undefined;
      }
      if (routeForCreate.model) overrides.model = routeForCreate.model;
      if (routeForCreate.reasoning_effort) overrides.reasoning_effort = routeForCreate.reasoning_effort;
      const session = await createSessionForProject(target.id, overrides);
      setSending(false);
      if (!session) {
        setError(createFailedText);
        return null;
      }
      return { sessionId: session.id, initialMessage: trimmed };
    },
    [sending, loaded, target, routeForCreate, agents, createSessionForProject, createFailedText],
  );

  const upsertSelectProject = useCallback(
    (project: WorkbenchProject) => {
      upsertProjectToTop(project); // updates the shared tree (sidebar + Projects page) too
      setSelectedId(project.id);
    },
    [upsertProjectToTop],
  );

  // Surface a project-load failure (provider-level) when we have no list, plus any
  // create error raised here.
  const visibleError = error ?? (!projectsLoaded && projectsError != null ? loadErrorText : null);

  return {
    projects,
    loaded,
    error: visibleError,
    sending,
    selectedId,
    setSelected: setSelectedId,
    target,
    needsProject,
    agents,
    defaultAgentName,
    effectiveDefaultAgentName,
    agentRoute,
    setAgentRoute: applyAgentRoute,
    send,
    upsertSelectProject,
  };
}
