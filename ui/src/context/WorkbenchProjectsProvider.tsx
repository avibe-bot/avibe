import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { useApi } from './ApiContext';
import type { ProjectDefaultAgent, WorkbenchProject, WorkbenchSession, WorkbenchSessionCreate } from './ApiContext';
import {
  WorkbenchProjectsContext,
  type ProjectSessionsState,
  type WorkbenchProjectsTree,
} from './WorkbenchProjectsContext';
import { createdReconcileMinCount } from '../lib/sessionVisibilityEvents';
import { orderProjectSessions } from '../lib/sessionPinning';
import { errorMessage } from '@/lib/errorMessage';
import {
  createWorkbenchSessionReadOwnership,
  type WorkbenchSessionReadStamp,
} from '../lib/workbenchSessionReadOwnership';

// How many sessions to load per page under a project. The server clamps the
// /api/sessions limit (to 200) and returns a cursor (next_before_id); both the
// desktop sidebar and the mobile Projects page append the next page via a
// "Load more" control rather than loading every session up front. Keep this at
// 8 so both surfaces expose the same compact first page before lazy loading
// longer histories. Both surfaces share a single per-project cache, so the page
// size has to be one shared value (it can't differ per surface).
const SESSIONS_PAGE_SIZE = 8;
const RECONNECT_SESSIONS_PAGE_SIZE = 200;

const EMPTY_SESSIONS: ProjectSessionsState = {
  sessions: null,
  loading: false,
  loadingMore: false,
  cursor: null,
  error: false,
};

// Scan every project's loaded rows for a session id and apply `patch`; returns a
// new state only when something actually changed (so unrelated consumers don't
// re-render). Used for both the status-dot and title SSE patches — keyed on
// session_id, so it doesn't depend on the server's scope_id format.
function patchSessionRow(
  prev: Record<string, ProjectSessionsState>,
  sessionId: string,
  patch: (session: WorkbenchSession) => WorkbenchSession,
  reorder = false,
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
    next[projectId] = rowChanged
      ? { ...state, sessions: reorder ? orderProjectSessions(rows) : rows }
      : state;
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
  // All reads owned by this provider share one ordering fence. A bootstrap,
  // paginated read, or targeted row read that started before a newer mutation
  // must not commit its snapshot after that mutation is accepted.
  const readOwnershipRef = useRef(createWorkbenchSessionReadOwnership());
  const authoritativeProjectsLoadedRef = useRef(false);
  const bootstrapRetryPendingRef = useRef(false);
  const projectTreeRetryPendingRef = useRef(false);

  const acceptSessionMutation = useCallback(() => {
    readOwnershipRef.current.acceptMutation();
  }, []);

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
    const read = readOwnershipRef.current.beginRead(['projects', 'projects-bootstrap']);
    const retryAfterMutation = () => {
      if (
        authoritativeProjectsLoadedRef.current ||
        readOwnershipRef.current.epoch() === read.epoch ||
        bootstrapRetryPendingRef.current
      ) {
        return;
      }
      bootstrapRetryPendingRef.current = true;
      queueMicrotask(() => {
        bootstrapRetryPendingRef.current = false;
        if (!authoritativeProjectsLoadedRef.current) void fetchProjects({ cache: false });
      });
    };
    try {
      const result = await api.getWorkbenchProjectsBootstrap({ cache: options?.cache });
      if (!readOwnershipRef.current.isCurrent(read, 'projects')) {
        retryAfterMutation();
        return;
      }
      setProjects(result.projects);
      const pages = Object.fromEntries(
        Object.entries(result.sessions ?? {}).filter(([projectId]) =>
          readOwnershipRef.current.isCurrent(read, `project:${projectId}`),
        ),
      );
      applyBootstrapSessions(pages);
      setProjectsError(null);
      authoritativeProjectsLoadedRef.current = true;
    } catch (err) {
      // Don't strand consumers on an empty-state for a transient failure — keep
      // any list we had and surface the error (mobile shows a retry).
      if (readOwnershipRef.current.isCurrent(read, 'projects')) {
        setProjectsError(errorMessage(err) ?? String(err));
      } else {
        retryAfterMutation();
      }
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
        const read = readOwnershipRef.current.beginRead(`project:${projectId}`);
        let stale = false;
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
            if (!readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`])) {
              stale = true;
              break;
            }
            for (const s of res.sessions) {
              if (!seen.has(s.id)) {
                seen.add(s.id);
                acc.push(s);
              }
            }
            nextBeforeId = res.next_before_id;
            before = res.next_before_id ?? undefined;
          } while (before && acc.length < targetCount);
          if (!stale && readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`])) {
            setSessions((prev) => ({
              ...prev,
              [projectId]: { sessions: acc, cursor: nextBeforeId, loading: false, loadingMore: false, error: false },
            }));
          }
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

  const queueReconcileForSession = useCallback(
    (sessionId: string) => {
      const entry = Object.entries(sessionsRef.current).find(([, state]) =>
        state.sessions?.some((session) => session.id === sessionId),
      );
      if (!entry) return;
      const [projectId, state] = entry;
      if (!inFlightRef.current.has(projectId)) return;
      queueReconcile(projectId, state.sessions?.length ?? 0);
    },
    [queueReconcile],
  );

  const reconcileProjectTree = useCallback(async function reconcileProjectTree() {
    const retryAfterInvalidation = (read: WorkbenchSessionReadStamp) => {
      if (
        readOwnershipRef.current.isCurrent(read, 'projects-bootstrap') ||
        !readOwnershipRef.current.isLatestRead(read) ||
        projectTreeRetryPendingRef.current
      ) {
        return;
      }
      projectTreeRetryPendingRef.current = true;
      queueMicrotask(() => {
        projectTreeRetryPendingRef.current = false;
        void reconcileProjectTree();
      });
    };
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
    const read = readOwnershipRef.current.beginRead(['projects', 'projects-bootstrap']);
    try {
      const groups = Array.from(bootstrapGroups.entries());
      if (groups.length === 0) {
        const result = await api.getWorkbenchProjectsBootstrap({ cache: false });
        if (readOwnershipRef.current.isCurrent(read, 'projects')) setProjects(result.projects);
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
          if (!readOwnershipRef.current.isCurrent(read, 'projects-bootstrap')) {
            retryAfterInvalidation(read);
            return;
          }
          nextProjects = result.projects;
          for (const [projectId, page] of Object.entries(result.sessions ?? {})) {
            const currentCount = sessionsRef.current[projectId]?.sessions?.length ?? 0;
            if (
              page &&
              !inFlightRef.current.has(projectId) &&
              currentCount <= limit &&
              readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`])
            ) {
              pages[projectId] = page;
            } else {
              queueReconcile(projectId, currentCount);
            }
          }
        }
        if (readOwnershipRef.current.isCurrent(read, 'projects') && nextProjects) setProjects(nextProjects);
        const currentPages = Object.fromEntries(
          Object.entries(pages).filter(([projectId]) =>
            readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`]),
          ),
        );
        applyBootstrapSessions(currentPages);
      }
      if (!readOwnershipRef.current.isCurrent(read, 'projects-bootstrap')) {
        retryAfterInvalidation(read);
        return;
      }
      if (readOwnershipRef.current.isCurrent(read, 'projects')) setProjectsError(null);
      for (const projectId of largeProjectIds) {
        void reconcileSessions(projectId);
      }
    } catch (err) {
      if (!readOwnershipRef.current.isCurrent(read, 'projects-bootstrap')) {
        retryAfterInvalidation(read);
        return;
      }
      if (readOwnershipRef.current.isCurrent(read, 'projects')) setProjectsError(errorMessage(err) ?? String(err));
      const projectIds = [...bootstrapGroups.values()].flat();
      for (const projectId of [...projectIds, ...largeProjectIds]) {
        void reconcileSessions(projectId);
      }
    }
  }, [api, applyBootstrapSessions, queueReconcile, reconcileSessions]);

  const refreshCachedSessionRow = useCallback(async (sessionId: string) => {
    const projectId = Object.entries(sessionsRef.current).find(([, state]) =>
      state.sessions?.some((session) => session.id === sessionId && !session.native_session_id),
    )?.[0];
    if (!projectId) return;
    const read = readOwnershipRef.current.beginRead(`project:${projectId}`);
    try {
      const updated = await api.getSession(sessionId, { cache: false });
      if (readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`])) {
        setSessions((prev) => patchSessionRow(prev, sessionId, () => updated));
      }
    } catch {
      /* best-effort: reconnect reconcile will refresh the row later */
    }
  }, [api]);

  // Load the first page (append=false) or the next page (append=true) of a
  // project's sessions, with dedupe + per-project serialisation.
  const fetchSessions = useCallback(
    async function fetchSessions(projectId: string, opts?: { append?: boolean }) {
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
      const beforeId = append ? sessionsRef.current[projectId]?.cursor ?? undefined : undefined;
      const read = readOwnershipRef.current.beginRead(`project:${projectId}`);
      let retryFirstPage = false;
      try {
        const res = await api.listSessions({ projectId, status: 'active', limit: SESSIONS_PAGE_SIZE, beforeId });
        const currentRead = readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`]);
        retryFirstPage = !currentRead && !append && sessionsRef.current[projectId]?.sessions === null;
        setSessions((prev) => {
          const cur = prev[projectId] ?? EMPTY_SESSIONS;
          if (!currentRead) {
            return {
              ...prev,
              [projectId]: append ? { ...cur, loadingMore: false } : { ...cur, loading: false },
            };
          }
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
        const currentRead = readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`]);
        retryFirstPage = !currentRead && !append && sessionsRef.current[projectId]?.sessions === null;
        setSessions((prev) => {
          const cur = prev[projectId] ?? EMPTY_SESSIONS;
          if (!readOwnershipRef.current.isLatestRead(read)) return prev;
          if (!currentRead) {
            return append
              ? { ...prev, [projectId]: { ...cur, loadingMore: false } }
              : { ...prev, [projectId]: { ...cur, loading: false } };
          }
          // Load-more failure: keep the list + cursor so the button stays usable.
          // First-page failure: flag error (mobile retry; re-expand refetches) and
          // keep `sessions` non-null so the desktop still renders its empty state.
          return append
            ? { ...prev, [projectId]: { ...cur, loadingMore: false } }
            : { ...prev, [projectId]: { ...cur, sessions: cur.sessions ?? [], loading: false, error: true } };
        });
      } finally {
        inFlightRef.current.delete(projectId);
        if (retryFirstPage) {
          queueMicrotask(() => {
            const current = sessionsRef.current[projectId];
            if (current?.sessions === null && !inFlightRef.current.has(projectId)) {
              void fetchSessions(projectId);
            }
          });
        }
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
        acceptSessionMutation();
        if (data.event === 'archived') {
          // Terminal archive (here or in another tab) — drop the row live.
          setSessions((prev) => removeSessionRow(prev, data.session_id));
          return;
        }
        if (data.event === 'updated') {
          const hasTitle = Object.prototype.hasOwnProperty.call(data, 'title');
          const hasPinned = typeof data.pinned === 'boolean';
          if (!hasTitle && !hasPinned) return;
          const nextTitle = data.title ?? null;
          setSessions((prev) =>
            patchSessionRow(
              prev,
              data.session_id,
              (session) => {
                const titleChanged = hasTitle && session.title !== nextTitle;
                const pinnedChanged = hasPinned && session.pinned !== data.pinned;
                if (!titleChanged && !pinnedChanged) return session;
                return {
                  ...session,
                  ...(titleChanged ? { title: nextTitle } : {}),
                  ...(pinnedChanged ? { pinned: data.pinned as boolean } : {}),
                };
              },
              hasPinned,
            ),
          );
          if (hasPinned) {
            const projectId = projectsRef.current?.find((project) => project.scope_id === data.scope_id)?.id;
            if (projectId) {
              const loaded = sessionsRef.current[projectId]?.sessions?.length ?? 0;
              void reconcileSessions(projectId, { minCount: loaded });
            }
          }
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
        acceptSessionMutation();
        queueReconcileForSession(session_id);
        setSessions((prev) =>
          patchSessionRow(prev, session_id, (s) => (s.agent_status === agent_status ? s : { ...s, agent_status })),
        );
        if (agent_status !== 'running') void refreshCachedSessionRow(session_id);
      },
      onTurnEnd: ({ session_id }) => {
        acceptSessionMutation();
        queueReconcileForSession(session_id);
        // The first turn can bind the native_session_id server-side, but the
        // status event only carries the dot state. Refresh the cached row so
        // actions gated on native binding, such as Fork session, unlock without
        // waiting for a full sidebar reload.
        void refreshCachedSessionRow(session_id);
      },
    });
    return disconnect;
  }, [
    acceptSessionMutation,
    api,
    queueReconcileForSession,
    reconcileProjectTree,
    reconcileSessions,
    refreshCachedSessionRow,
  ]);

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
        acceptSessionMutation();
        if (alreadyLoaded) {
          setSessions((prev) => {
            const cur = prev[projectId] ?? EMPTY_SESSIONS;
            const rows = cur.sessions ?? [];
            return {
              ...prev,
              [projectId]: {
                ...cur,
                sessions: orderProjectSessions([session, ...rows.filter((s) => s.id !== session.id)]),
              },
            };
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
    [acceptSessionMutation, api, fetchSessions],
  );

  const renameProject = useCallback(
    async (projectId: string, name: string) => {
      try {
        const updated = await api.updateProject(projectId, { display_name: name });
        acceptSessionMutation();
        setProjects((prev) => (prev ? prev.map((p) => (p.id === projectId ? updated : p)) : prev));
      } catch (err) {
        console.error('[workbench] rename project failed', err);
      }
    },
    [acceptSessionMutation, api],
  );

  const forkSession = useCallback(
    async (projectId: string | null, sessionId: string): Promise<WorkbenchSession | null> => {
      // A standalone session (no project-bound scope, so `project_id: null`) has no
      // bucket in this tree. The fork itself is session-keyed, so it still runs —
      // only the cache placement below is skipped, otherwise "fork" would silently
      // do nothing for every session outside a project (Codex).
      const alreadyLoaded = projectId != null && sessionsRef.current[projectId]?.sessions != null;
      try {
        const session = await api.forkSession(sessionId);
        acceptSessionMutation();
        if (projectId == null) return session;
        if (alreadyLoaded) {
          setSessions((prev) => {
            const cur = prev[projectId] ?? EMPTY_SESSIONS;
            const rows = cur.sessions ?? [];
            return {
              ...prev,
              [projectId]: {
                ...cur,
                sessions: orderProjectSessions([session, ...rows.filter((s) => s.id !== session.id)]),
              },
            };
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
    [acceptSessionMutation, api, fetchSessions],
  );

  const setProjectDefaultAgent = useCallback(
    async (projectId: string, route: ProjectDefaultAgent, expectedAgentId: string | null) => {
      // Always send the full 5-field route: a complete set is coherent whether
      // the user picked an agent (all set) or cleared it (all null → default
      // dropped). Let failures propagate — apiFetch already toasted.
      const updated = await api.updateProject(projectId, {
        agent_id: route.agent_id,
        expected_agent_id: expectedAgentId,
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
        acceptSessionMutation();
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
    [acceptSessionMutation, api],
  );

  const renameSession = useCallback(
    async (projectId: string, sessionId: string, title: string) => {
      // Empty string clears to "untitled" server-side. Patch from the REST
      // response so the row updates even if the session.activity SSE drops; the
      // broadcast then reconciles the same value. Throws on failure (caller's
      // inline editor catches it) so a failed rename leaves the old title.
      const updated = await api.updateSession(sessionId, { title });
      acceptSessionMutation();
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
    [acceptSessionMutation, api],
  );

  const setSessionPinned = useCallback(
    async (projectId: string | null, sessionId: string, pinned: boolean) => {
      const updated = await api.updateSession(sessionId, { pinned });
      acceptSessionMutation();
      // Session-keyed: patches whatever project holds the row (none, for a
      // standalone session). Only the pinned-first re-order needs a project.
      setSessions((prev) => patchSessionRow(prev, sessionId, () => updated, true));
      if (projectId == null) return;
      const loaded = sessionsRef.current[projectId]?.sessions?.length ?? 0;
      void reconcileSessions(projectId, { minCount: loaded });
    },
    [acceptSessionMutation, api, reconcileSessions],
  );

  const archiveSession = useCallback(
    async (projectId: string | null, sessionId: string) => {
      // Archive is terminal — the API reclaims bound tasks/watches/runs server-side.
      // Drop the row from the tree on success; throw so the caller's dialog can react.
      await api.archiveSession(sessionId);
      acceptSessionMutation();
      if (projectId == null) return; // standalone session: no tree row to drop
      setSessions((prev) => {
        const state = prev[projectId];
        if (!state?.sessions) return prev;
        return {
          ...prev,
          [projectId]: { ...state, sessions: state.sessions.filter((s) => s.id !== sessionId) },
        };
      });
    },
    [acceptSessionMutation, api],
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
      setSessionPinned,
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
      setSessionPinned,
      archiveSession,
      upsertProjectToTop,
    ],
  );

  return <WorkbenchProjectsContext.Provider value={value}>{children}</WorkbenchProjectsContext.Provider>;
};
