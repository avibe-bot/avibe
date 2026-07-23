import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { useApi } from './ApiContext';
import type { ProjectDefaultAgent, WorkbenchProject, WorkbenchSession, WorkbenchSessionCreate } from './ApiContext';
import { createdReconcileMinCount } from '../lib/sessionVisibilityEvents';

// How many sessions to load per page under a project. The server clamps the
// /api/sessions limit (to 200) and returns a cursor (next_before_id); both the
// desktop sidebar and the mobile Projects page append the next page via a
// "Load more" control rather than loading every session up front. Keep this at
// 8 so both surfaces expose the same compact first page before lazy loading
// longer histories. Both surfaces share a single per-project cache, so the page
// size has to be one shared value (it can't differ per surface).
const SESSIONS_PAGE_SIZE = 8;
const RECONNECT_SESSIONS_PAGE_SIZE = 200;

export interface ProjectSessionsState {
  /** null = not loaded yet. [] = loaded-but-empty (or a first-page failure, with `error`). */
  sessions: WorkbenchSession[] | null;
  /** First-page (or retry) fetch in flight. */
  loading: boolean;
  /** Load-more (append) fetch in flight. */
  loadingMore: boolean;
  /** next_before_id: a string means more pages exist, null means fully loaded. */
  cursor: string | null;
  /** The last first-page fetch failed — any rows are kept so the user can retry. */
  error: boolean;
}

const EMPTY_SESSIONS: ProjectSessionsState = {
  sessions: null,
  loading: false,
  loadingMore: false,
  cursor: null,
  error: false,
};

export interface WorkbenchProjectsTree {
  projects: WorkbenchProject[] | null;
  projectsError: string | null;
  refreshProjects: () => Promise<void>;

  sessionsOf: (projectId: string) => ProjectSessionsState;
  expanded: ReadonlySet<string>;
  isExpanded: (projectId: string) => boolean;
  toggleExpanded: (projectId: string) => void;
  loadMore: (projectId: string) => void;
  /** Re-fetch the first page (mobile retry button / programmatic reload). */
  reloadSessions: (projectId: string) => void;

  creatingSession: (projectId: string) => boolean;
  /** Creates a session under a project (optimistic prepend + expand) and RETURNS it;
   *  the caller navigates (this provider is mounted outside the router). null on failure.
   *  `overrides` lets the create surfaces pin an agent/backend; omit for the server default. */
  createSessionForProject: (projectId: string, overrides?: Partial<WorkbenchSessionCreate>) => Promise<WorkbenchSession | null>;
  /** Fork an existing session, prepend the new row to the source project, and return it for navigation. */
  forkSession: (projectId: string, sessionId: string) => Promise<WorkbenchSession | null>;
  renameProject: (projectId: string, name: string) => Promise<void>;
  /** Persist the project's default Agent route (Project Settings) and patch the
   *  shared cache so the sidebar + Projects page reflect it. Pass an all-null
   *  route to clear the default back to the global default. Throws on failure
   *  (the apiFetch layer already surfaced a toast) so the dialog can react. */
  setProjectDefaultAgent: (projectId: string, route: ProjectDefaultAgent) => Promise<void>;
  archiveProject: (projectId: string) => Promise<void>;
  /** Throws on failure so the row's inline editor can fall back; patches title on success. */
  renameSession: (projectId: string, sessionId: string, title: string) => Promise<void>;
  /** Permanently archive a session: calls the API (which reclaims its bound
   *  tasks/watches/runs) then drops the row from the tree. Throws on failure. */
  archiveSession: (projectId: string, sessionId: string) => Promise<void>;
  /** After NewProjectDialog: dedup-by-id, hoist to top, expand, fetch sessions if not loaded. */
  upsertProjectToTop: (project: WorkbenchProject) => void;
}

// Scan every project's loaded rows for a session id and apply `patch`; returns a
// new state only when something actually changed (so unrelated consumers don't
// re-render). Used for both the status-dot and title SSE patches — keyed on
// session_id, so it doesn't depend on the server's scope_id format.
function patchSessionRow(
  prev: Record<string, ProjectSessionsState>,
  sessionId: string,
  patch: (session: WorkbenchSession) => WorkbenchSession,
): Record<string, ProjectSessionsState> {
  let changed = false;
  const next: Record<string, ProjectSessionsState> = {};
  for (const [projectId, state] of Object.entries(prev)) {
    if (!state.sessions) {
      next[projectId] = state;
      continue;
    }
    let rowChanged = false;
    const rows = state.sessions.map((s) => {
      if (s.id !== sessionId) return s;
      const updated = patch(s);
      if (updated !== s) rowChanged = true;
      return updated;
    });
    next[projectId] = rowChanged ? { ...state, sessions: rows } : state;
    if (rowChanged) changed = true;
  }
  return changed ? next : prev;
}

// Drop a session id from every project's loaded rows — used when an archive
// broadcast (possibly from another tab) should remove the row live. Returns a
// new state only when a row was actually removed.
function removeSessionRow(
  prev: Record<string, ProjectSessionsState>,
  sessionId: string,
): Record<string, ProjectSessionsState> {
  let changed = false;
  const next: Record<string, ProjectSessionsState> = {};
  for (const [projectId, state] of Object.entries(prev)) {
    if (!state.sessions) {
      next[projectId] = state;
      continue;
    }
    const rows = state.sessions.filter((s) => s.id !== sessionId);
    if (rows.length !== state.sessions.length) {
      next[projectId] = { ...state, sessions: rows };
      changed = true;
    } else {
      next[projectId] = state;
    }
  }
  return changed ? next : prev;
}

const REORDER_ACTIVITY_EVENTS = new Set(['created', 'user_message', 'show_event']);

const WorkbenchProjectsContext = createContext<WorkbenchProjectsTree | null>(null);

// Single source of truth for the workbench projects/sessions tree. The desktop
// WorkbenchSidebar (always mounted) and the mobile ProjectsPage (route) both
// consume it, so it's a PROVIDER (one EventSource + one cache) rather than a
// per-consumer hook — mirroring WorkbenchInboxContext, which made the same call
// for the same "sidebar + page both need live SSE data" situation. Owns: load +
// paginate + dedupe sessions, reconnect reconcile (chunked, survives the 200-row
// server clamp), live status/title via SSE, create/rename/archive. Navigation
// stays in consumers — this is mounted outside <RouterProvider>. Unread stays in
// WorkbenchInboxContext (both consumers read it directly).
export const WorkbenchProjectsProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const api = useApi();
  const [projects, setProjects] = useState<WorkbenchProject[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [sessions, setSessions] = useState<Record<string, ProjectSessionsState>>({});
  const [creating, setCreating] = useState<Set<string>>(new Set());

  // Stale-closure-safe mirrors so the SSE (re)connect reconcile reads the current
  // expanded set + loaded window without re-subscribing the stream on every change.
  const projectsRef = useRef<WorkbenchProject[] | null>(null);
  projectsRef.current = projects;
  const sessionsRef = useRef<Record<string, ProjectSessionsState>>({});
  sessionsRef.current = sessions;
  const expandedRef = useRef<Set<string>>(new Set());
  expandedRef.current = expanded;
  // Projects with an in-flight session fetch — serialises first-page / load-more /
  // reconcile per project so they can't race or truncate an append.
  const inFlightRef = useRef<Set<string>>(new Set());
  const pendingReconcileRef = useRef<Map<string, number>>(new Map());

  const queueReconcile = useCallback((projectId: string, minCount = 0) => {
    const pending = pendingReconcileRef.current.get(projectId) ?? 0;
    pendingReconcileRef.current.set(projectId, Math.max(pending, minCount));
  }, []);

  const takePendingReconcile = useCallback((projectId: string): number | null => {
    const pending = pendingReconcileRef.current.get(projectId);
    if (pending === undefined) return null;
    pendingReconcileRef.current.delete(projectId);
    return pending;
  }, []);

  const applyBootstrapSessions = useCallback((pages: Record<string, { sessions: WorkbenchSession[]; next_before_id: string | null } | undefined>) => {
    setSessions((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [projectId, page] of Object.entries(pages)) {
        if (!page) continue;
        next[projectId] = {
          sessions: page.sessions,
          cursor: page.next_before_id,
          loading: false,
          loadingMore: false,
          error: false,
        };
        changed = true;
      }
      return changed ? next : prev;
    });
  }, []);

  const fetchProjects = useCallback(async (options?: { cache?: boolean }) => {
    try {
      const result = await api.getWorkbenchProjectsBootstrap({ cache: options?.cache });
      setProjects(result.projects);
      applyBootstrapSessions(result.sessions ?? {});
      setProjectsError(null);
    } catch (err: any) {
      // Don't strand consumers on an empty-state for a transient failure — keep
      // any list we had and surface the error (mobile shows a retry).
      setProjectsError(err?.message ?? String(err));
    }
  }, [api, applyBootstrapSessions]);

  useEffect(() => {
    void fetchProjects();
  }, [fetchProjects]);

  // (Re)connect reconcile: rebuild a project's ALREADY-paged-in window (not just
  // page 1) so a transient SSE reconnect / controller restart doesn't truncate an
  // expanded project back to the first page. Pages in chunks because the server
  // clamps the limit to 200 — a single large request would silently truncate
  // windows >200 rows. Silent (no loading flag) so visible rows don't flicker.
  const reconcileSessions = useCallback(
    async (projectId: string, opts?: { minCount?: number }) => {
      if (inFlightRef.current.has(projectId)) {
        queueReconcile(projectId, opts?.minCount ?? 0);
        return;
      }
      let minCount = opts?.minCount ?? 0;
      while (true) {
        const targetCount = Math.max(sessionsRef.current[projectId]?.sessions?.length ?? 0, minCount);
        if (targetCount === 0) return; // nothing loaded to reconcile
        inFlightRef.current.add(projectId);
        try {
          const acc: WorkbenchSession[] = [];
          const seen = new Set<string>();
          let before: string | undefined;
          let nextBeforeId: string | null = null;
          do {
            const res = await api.listSessions({
              projectId,
              status: 'active',
              limit: SESSIONS_PAGE_SIZE,
              beforeId: before,
              cache: false,
            });
            for (const s of res.sessions) {
              if (!seen.has(s.id)) {
                seen.add(s.id);
                acc.push(s);
              }
            }
            nextBeforeId = res.next_before_id;
            before = res.next_before_id ?? undefined;
          } while (before && acc.length < targetCount);
          setSessions((prev) => ({
            ...prev,
            [projectId]: { sessions: acc, cursor: nextBeforeId, loading: false, loadingMore: false, error: false },
          }));
        } catch {
          /* keep the current window on a failed reconcile */
        } finally {
          inFlightRef.current.delete(projectId);
        }
        const pendingMinCount = takePendingReconcile(projectId);
        if (pendingMinCount === null) return;
        minCount = pendingMinCount;
      }
    },
    [api, queueReconcile, takePendingReconcile],
  );

  const reconcileProjectTree = useCallback(async () => {
    const bootstrapGroups = new Map<number, string[]>();
    const largeProjectIds: string[] = [];
    for (const [projectId, state] of Object.entries(sessionsRef.current)) {
      if (!state || state.sessions === null) continue;
      const loadedCount = state.sessions.length;
      if (inFlightRef.current.has(projectId)) {
        queueReconcile(projectId, loadedCount);
        continue;
      }
      if (state.sessions.length > RECONNECT_SESSIONS_PAGE_SIZE) {
        largeProjectIds.push(projectId);
        continue;
      }
      const limit = Math.max(SESSIONS_PAGE_SIZE, Math.min(RECONNECT_SESSIONS_PAGE_SIZE, loadedCount || SESSIONS_PAGE_SIZE));
      const group = bootstrapGroups.get(limit) ?? [];
      group.push(projectId);
      bootstrapGroups.set(limit, group);
    }
    try {
      const groups = Array.from(bootstrapGroups.entries());
      if (groups.length === 0) {
        const result = await api.getWorkbenchProjectsBootstrap({ cache: false });
        setProjects(result.projects);
      } else {
        let nextProjects: WorkbenchProject[] | null = null;
        const pages: Record<string, { sessions: WorkbenchSession[]; next_before_id: string | null }> = {};
        for (const [limit, projectIds] of groups) {
          const result = await api.getWorkbenchProjectsBootstrap({
            projectIds,
            status: 'active',
            limit,
            cache: false,
          });
          nextProjects = result.projects;
          for (const [projectId, page] of Object.entries(result.sessions ?? {})) {
            const currentCount = sessionsRef.current[projectId]?.sessions?.length ?? 0;
            if (page && !inFlightRef.current.has(projectId) && currentCount <= limit) {
              pages[projectId] = page;
            } else {
              queueReconcile(projectId, currentCount);
            }
          }
        }
        if (nextProjects) setProjects(nextProjects);
        applyBootstrapSessions(pages);
      }
      setProjectsError(null);
      for (const projectId of largeProjectIds) {
        void reconcileSessions(projectId);
      }
    } catch (err: any) {
      setProjectsError(err?.message ?? String(err));
      const projectIds = [...bootstrapGroups.values()].flat();
      for (const projectId of [...projectIds, ...largeProjectIds]) {
        void reconcileSessions(projectId);
      }
    }
  }, [api, applyBootstrapSessions, queueReconcile, reconcileSessions]);

  const refreshCachedSessionRow = useCallback(async (sessionId: string) => {
    const needsRefresh = Object.values(sessionsRef.current).some((state) =>
      state.sessions?.some((session) => session.id === sessionId && !session.native_session_id),
    );
    if (!needsRefresh) return;
    try {
      const updated = await api.getSession(sessionId, { cache: false });
      setSessions((prev) => patchSessionRow(prev, sessionId, () => updated));
    } catch {
      /* best-effort: reconnect reconcile will refresh the row later */
    }
  }, [api]);

  // Load the first page (append=false) or the next page (append=true) of a
  // project's sessions, with dedupe + per-project serialisation.
  const fetchSessions = useCallback(
    async (projectId: string, opts?: { append?: boolean }) => {
      const append = opts?.append ?? false;
      if (append && !sessionsRef.current[projectId]?.cursor) return; // nothing more to load
      if (inFlightRef.current.has(projectId)) return; // serialise per project
      inFlightRef.current.add(projectId);
      setSessions((prev) => {
        const cur = prev[projectId] ?? EMPTY_SESSIONS;
        return {
          ...prev,
          [projectId]: append ? { ...cur, loadingMore: true } : { ...cur, loading: true, error: false },
        };
      });
      try {
        const beforeId = append ? sessionsRef.current[projectId]?.cursor ?? undefined : undefined;
        const res = await api.listSessions({ projectId, status: 'active', limit: SESSIONS_PAGE_SIZE, beforeId });
        setSessions((prev) => {
          const cur = prev[projectId] ?? EMPTY_SESSIONS;
          const existing = append ? cur.sessions ?? [] : [];
          // Cursor pages can overlap if a row's last_active_at shifts between
          // fetches (the cursor is just a row id resolved against current
          // activity); drop ids we already hold so rows never duplicate.
          const seen = new Set(existing.map((s) => s.id));
          const merged = [...existing, ...res.sessions.filter((s) => !seen.has(s.id))];
          return {
            ...prev,
            [projectId]: { sessions: merged, cursor: res.next_before_id, loading: false, loadingMore: false, error: false },
          };
        });
      } catch {
        setSessions((prev) => {
          const cur = prev[projectId] ?? EMPTY_SESSIONS;
          // Load-more failure: keep the list + cursor so the button stays usable.
          // First-page failure: flag error (mobile retry; re-expand refetches) and
          // keep `sessions` non-null so the desktop still renders its empty state.
          return append
            ? { ...prev, [projectId]: { ...cur, loadingMore: false } }
            : { ...prev, [projectId]: { ...cur, sessions: cur.sessions ?? [], loading: false, error: true } };
        });
      } finally {
        inFlightRef.current.delete(projectId);
        const pendingMinCount = takePendingReconcile(projectId);
        if (pendingMinCount !== null) {
          void reconcileSessions(projectId, { minCount: pendingMinCount });
        }
      }
    },
    [api, reconcileSessions, takePendingReconcile],
  );

  // Keep the tree live: patch a row's status dot / title from SSE, and refetch
  // projects + every loaded project's window when the stream (re)opens (the
  // crash-recovery reset that runs server-side during a drop has no subscriber to
  // broadcast to, so listSessions is the authoritative source on reconnect).
  useEffect(() => {
    const disconnect = api.connectWorkbenchEvents({
      onConnected: () => {
        void reconcileProjectTree();
      },
      onSessionActivity: (data) => {
        if (data.event === 'archived') {
          // Terminal archive (here or in another tab) — drop the row live.
          setSessions((prev) => removeSessionRow(prev, data.session_id));
          return;
        }
        if (data.event === 'updated' && Object.prototype.hasOwnProperty.call(data, 'title')) {
          const nextTitle = data.title ?? null;
          setSessions((prev) =>
            patchSessionRow(prev, data.session_id, (s) => (s.title === nextTitle ? s : { ...s, title: nextTitle })),
          );
          return;
        }
        if (!REORDER_ACTIVITY_EVENTS.has(data.event)) return;
        const projectId = projectsRef.current?.find((project) => project.scope_id === data.scope_id)?.id;
        if (!projectId) return;
        // Grow the window ONLY for a synthesized foreground-restore (`data.restored`,
        // set by visibilityActivityEvents on Undo): reconcile one past the loaded
        // page so a restored row ranked just past it returns (a flat minCount 1 stops
        // at the first page → Undo looks broken). A real backend `created` (a new
        // session, never marked) keeps the original minCount 1 — otherwise repeated
        // local create/fork, which already prepend the row before this event fires,
        // would inflate the window by one each time (Codex r3). See createdReconcileMinCount.
        const loaded = sessionsRef.current[projectId]?.sessions?.length ?? 0;
        const minCount =
          data.event === 'created' ? createdReconcileMinCount(!!data.restored, loaded) : 0;
        void reconcileSessions(projectId, { minCount });
      },
      onSessionStatus: ({ session_id, agent_status }) => {
        setSessions((prev) =>
          patchSessionRow(prev, session_id, (s) => (s.agent_status === agent_status ? s : { ...s, agent_status })),
        );
        if (agent_status !== 'running') void refreshCachedSessionRow(session_id);
      },
      onTurnEnd: ({ session_id }) => {
        // The first turn can bind the native_session_id server-side, but the
        // status event only carries the dot state. Refresh the cached row so
        // actions gated on native binding, such as Fork session, unlock without
        // waiting for a full sidebar reload.
        void refreshCachedSessionRow(session_id);
      },
    });
    return disconnect;
  }, [api, reconcileProjectTree, reconcileSessions, refreshCachedSessionRow]);

  const toggleExpanded = useCallback(
    (projectId: string) => {
      const willExpand = !expandedRef.current.has(projectId);
      setExpanded((prev) => {
        const next = new Set(prev);
        if (willExpand) next.add(projectId);
        else next.delete(projectId);
        return next;
      });
      if (willExpand) {
        // Fetch the first page if never loaded or the last load failed (a healthy
        // loaded project keeps the pages the user already paged in).
        const state = sessionsRef.current[projectId];
        if (!state || state.sessions === null || state.error) void fetchSessions(projectId);
      }
    },
    [fetchSessions],
  );

  const createSessionForProject = useCallback(
    async (projectId: string, overrides?: Partial<WorkbenchSessionCreate>): Promise<WorkbenchSession | null> => {
      setCreating((prev) => new Set(prev).add(projectId));
      // Whether this project's list is already cached. If not, we must NOT seed a
      // partial cache: toggleExpanded treats any loaded entry as "already loaded"
      // and would never fetch the project's existing sessions, hiding them.
      const alreadyLoaded = sessionsRef.current[projectId]?.sessions != null;
      try {
        // No overrides → omit agent fields so the server defers to the default Agent.
        const session = await api.createSession({ project_id: projectId, ...overrides });
        if (alreadyLoaded) {
          setSessions((prev) => {
            const cur = prev[projectId] ?? EMPTY_SESSIONS;
            const rows = cur.sessions ?? [];
            return { ...prev, [projectId]: { ...cur, sessions: [session, ...rows.filter((s) => s.id !== session.id)] } };
          });
        }
        setExpanded((prev) => {
          if (prev.has(projectId)) return prev;
          const next = new Set(prev);
          next.add(projectId);
          return next;
        });
        if (!alreadyLoaded) void fetchSessions(projectId); // load the full list incl. the new one
        return session;
      } catch (err) {
        console.error('[workbench] create session failed', err);
        return null;
      } finally {
        setCreating((prev) => {
          const next = new Set(prev);
          next.delete(projectId);
          return next;
        });
      }
    },
    [api, fetchSessions],
  );

  const renameProject = useCallback(
    async (projectId: string, name: string) => {
      try {
        const updated = await api.updateProject(projectId, { display_name: name });
        setProjects((prev) => (prev ? prev.map((p) => (p.id === projectId ? updated : p)) : prev));
      } catch (err) {
        console.error('[workbench] rename project failed', err);
      }
    },
    [api],
  );

  const forkSession = useCallback(
    async (projectId: string, sessionId: string): Promise<WorkbenchSession | null> => {
      const alreadyLoaded = sessionsRef.current[projectId]?.sessions != null;
      try {
        const session = await api.forkSession(sessionId);
        if (alreadyLoaded) {
          setSessions((prev) => {
            const cur = prev[projectId] ?? EMPTY_SESSIONS;
            const rows = cur.sessions ?? [];
            return { ...prev, [projectId]: { ...cur, sessions: [session, ...rows.filter((s) => s.id !== session.id)] } };
          });
        }
        setExpanded((prev) => {
          if (prev.has(projectId)) return prev;
          const next = new Set(prev);
          next.add(projectId);
          return next;
        });
        if (!alreadyLoaded) void fetchSessions(projectId);
        return session;
      } catch (err) {
        console.error('[workbench] fork session failed', err);
        return null;
      }
    },
    [api, fetchSessions],
  );

  const setProjectDefaultAgent = useCallback(
    async (projectId: string, route: ProjectDefaultAgent) => {
      // Always send the full 5-field route: a complete set is coherent whether
      // the user picked an agent (all set) or cleared it (all null → default
      // dropped). Let failures propagate — apiFetch already toasted.
      const updated = await api.updateProject(projectId, {
        agent_name: route.agent_name,
        agent_variant: route.agent_variant,
        model: route.model,
        reasoning_effort: route.reasoning_effort,
      });
      setProjects((prev) => (prev ? prev.map((p) => (p.id === projectId ? updated : p)) : prev));
    },
    [api],
  );

  const archiveProject = useCallback(
    async (projectId: string) => {
      try {
        await api.archiveProject(projectId);
        setProjects((prev) => (prev ? prev.filter((p) => p.id !== projectId) : prev));
        setExpanded((prev) => {
          if (!prev.has(projectId)) return prev;
          const next = new Set(prev);
          next.delete(projectId);
          return next;
        });
      } catch (err) {
        console.error('[workbench] archive project failed', err);
      }
    },
    [api],
  );

  const renameSession = useCallback(
    async (projectId: string, sessionId: string, title: string) => {
      // Empty string clears to "untitled" server-side. Patch from the REST
      // response so the row updates even if the session.activity SSE drops; the
      // broadcast then reconciles the same value. Throws on failure (caller's
      // inline editor catches it) so a failed rename leaves the old title.
      const updated = await api.updateSession(sessionId, { title });
      setSessions((prev) => {
        const state = prev[projectId];
        if (!state?.sessions) return prev;
        return {
          ...prev,
          [projectId]: {
            ...state,
            sessions: state.sessions.map((s) => (s.id === sessionId ? { ...s, title: updated.title } : s)),
          },
        };
      });
    },
    [api],
  );

  const archiveSession = useCallback(
    async (projectId: string, sessionId: string) => {
      // Archive is terminal — the API reclaims bound tasks/watches/runs server-side.
      // Drop the row from the tree on success; throw so the caller's dialog can react.
      await api.archiveSession(sessionId);
      setSessions((prev) => {
        const state = prev[projectId];
        if (!state?.sessions) return prev;
        return {
          ...prev,
          [projectId]: { ...state, sessions: state.sessions.filter((s) => s.id !== sessionId) },
        };
      });
    },
    [api],
  );

  const upsertProjectToTop = useCallback(
    (project: WorkbenchProject) => {
      // create_project is find-or-create by path: opening a tracked folder returns
      // the existing project, refreshed. Drop any stale copy, hoist to top, expand.
      setProjects((prev) => (prev ? [project, ...prev.filter((p) => p.id !== project.id)] : [project]));
      setExpanded((prev) => {
        const next = new Set(prev);
        next.add(project.id);
        return next;
      });
      // New / restored project → load its real list; an already-open one keeps its
      // paged-in window instead of being truncated to the first page.
      const state = sessionsRef.current[project.id];
      if (!state || state.sessions === null || state.error) void fetchSessions(project.id);
    },
    [fetchSessions],
  );

  const sessionsOf = useCallback((projectId: string) => sessions[projectId] ?? EMPTY_SESSIONS, [sessions]);
  const isExpanded = useCallback((projectId: string) => expanded.has(projectId), [expanded]);
  const creatingSession = useCallback((projectId: string) => creating.has(projectId), [creating]);
  const loadMore = useCallback((projectId: string) => void fetchSessions(projectId, { append: true }), [fetchSessions]);
  const reloadSessions = useCallback((projectId: string) => void fetchSessions(projectId), [fetchSessions]);

  const value = useMemo<WorkbenchProjectsTree>(
    () => ({
      projects,
      projectsError,
      refreshProjects: fetchProjects,
      sessionsOf,
      expanded,
      isExpanded,
      toggleExpanded,
      loadMore,
      reloadSessions,
      creatingSession,
      createSessionForProject,
      forkSession,
      renameProject,
      setProjectDefaultAgent,
      archiveProject,
      renameSession,
      archiveSession,
      upsertProjectToTop,
    }),
    [
      projects,
      projectsError,
      fetchProjects,
      sessionsOf,
      expanded,
      isExpanded,
      toggleExpanded,
      loadMore,
      reloadSessions,
      creatingSession,
      createSessionForProject,
      forkSession,
      renameProject,
      setProjectDefaultAgent,
      archiveProject,
      renameSession,
      archiveSession,
      upsertProjectToTop,
    ],
  );

  return <WorkbenchProjectsContext.Provider value={value}>{children}</WorkbenchProjectsContext.Provider>;
};

export function useWorkbenchProjectsTree(): WorkbenchProjectsTree {
  const ctx = useContext(WorkbenchProjectsContext);
  if (!ctx) throw new Error('useWorkbenchProjectsTree must be used within a WorkbenchProjectsProvider');
  return ctx;
}
