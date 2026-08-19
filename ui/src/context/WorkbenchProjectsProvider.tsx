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
import { useCoalescedWrite } from '../lib/useCoalescedWrite';
import { errorMessage } from '@/lib/errorMessage';
import { useConsumerActivation } from '@/lib/useConsumerActivation';
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

// What a route pick means for the cached row: an all-null route is the user
// CLEARING the default, which the row carries as no default agent at all. One
// helper because two places have to agree — the optimistic write and the overlay
// that defends it against incoming rows.
const appliedRoute = (route: ProjectDefaultAgent): ProjectDefaultAgent | null => {
  const cleared = !(
    route.agent_id || route.agent_name || route.agent_variant || route.model || route.reasoning_effort
  );
  return cleared ? null : route;
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
  // The tree is workbench-only data behind an app-level provider. Nothing under
  // /admin renders a project, so nothing there should pay for the bootstrap —
  // neither on load nor on an SSE reconnect.
  const { active, isActive, activate } = useConsumerActivation();
  const [projects, setProjects] = useState<WorkbenchProject[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [sessions, setSessions] = useState<Record<string, ProjectSessionsState>>({});
  const [creating, setCreating] = useState<Set<string>>(new Set());

  // Stale-closure-safe mirrors so the SSE (re)connect reconcile reads the current
  // expanded set + loaded window without re-subscribing the stream on every change.
  // ``projectsRef`` is deliberately NOT assigned on render like the two below:
  // the reads consult it BEFORE issuing a request (see ``canIssueRead``), so it has
  // to be true when a write returns rather than one render later. ``commitProjects``
  // owns it together with the state; nothing else writes either half.
  const projectsRef = useRef<WorkbenchProject[] | null>(null);
  const sessionsRef = useRef<Record<string, ProjectSessionsState>>({});
  sessionsRef.current = sessions;
  const expandedRef = useRef<Set<string>>(new Set());
  expandedRef.current = expanded;
  // Projects with an in-flight session fetch — serialises first-page / load-more /
  // reconcile per project so they can't race or truncate an append.
  const inFlightRef = useRef<Set<string>>(new Set());
  const pendingReconcileRef = useRef<Map<string, number>>(new Map());
  const sessionProjectRef = useRef<Map<string, string>>(new Map());
  const cachedRowRefreshInFlightRef = useRef<Set<string>>(new Set());
  const pendingCachedRowRefreshRef = useRef<Set<string>>(new Set());
  // All reads owned by this provider share one ordering fence. A bootstrap,
  // paginated read, or targeted row read that started before a newer mutation
  // must not commit its snapshot after that mutation is accepted.
  const readOwnershipRef = useRef(createWorkbenchSessionReadOwnership());
  // Initial/manual project fetches and reconnect bootstraps claim the same
  // global resources. Serialize them and retain trailing intents so a later
  // failed recovery cannot invalidate an earlier successful snapshot.
  const bootstrapReadInFlightRef = useRef(false);
  // Whether this document has ever loaded the tree, which is what decides the
  // read a returning consumer takes (see the activation effect below).
  const treeInitialFetched = useRef(false);
  const fetchProjectsPendingRef = useRef<{ cache?: boolean } | null>(null);
  const projectTreePendingRef = useRef(false);
  // The newest default-Agent pick per project, so an earlier write's response can
  // be recognised as already outdated by the time it arrives.
  const latestProjectRouteRef = useRef(new Map<string, ProjectDefaultAgent>());
  // The Agent each project was last CONFIRMED to hold by the server — the token a
  // compare-and-set route write has to expect. It cannot be read off the list
  // below, because that list also carries picks the server has not accepted yet,
  // and expecting one of those is what turns a rejected pick into a deterministic
  // ``project_agent_conflict`` on the retry.
  const confirmedProjectAgentRef = useRef(new Map<string, string | null>());
  // The route each in-flight burst REPLACED, which is where a rejected burst goes
  // back to. Recorded when the burst opens, because that is the last moment the
  // cache still holds it.
  const preBurstRouteRef = useRef(new Map<string, ProjectDefaultAgent | null>());
  const fetchProjectsRunnerRef = useRef<(options?: { cache?: boolean }) => void>(() => {});
  const projectTreeRunnerRef = useRef<() => void>(() => {});

  // The list and its mirror move together. The mirror used to be written on render,
  // so after a write that added a project it stayed one render behind — which is why
  // the reads below could only check "does the tree still hold this project?" AFTER
  // their request and discard the response, and why asking beforehand would have
  // dropped the window a just-created project needs. A read cannot decline a request
  // on an answer it can only trust afterwards.
  //
  // ``confirmed`` names the rows THIS commit takes from the server, which is also
  // where the compare-and-set token for a route write comes from. Declared per
  // commit rather than inferred from the list, because a commit can install a
  // server row into a list that still holds another project's optimistic pick —
  // and recording that pick as confirmed is exactly the mistake the token exists
  // to prevent. Omitting it records nothing, so the cost of a future commit
  // forgetting is a stale token (one loud conflict, then a re-read) rather than a
  // wrong one.
  const commitProjects = useCallback(
    (
      next:
        | WorkbenchProject[]
        | null
        | ((prev: WorkbenchProject[] | null) => WorkbenchProject[] | null),
      options?: { confirmed?: WorkbenchProject[] | null },
    ) => {
      const requested = typeof next === 'function' ? next(projectsRef.current) : next;
      // ── The in-flight pick outranks every incoming row ────────────────────
      // A read that STARTS after a pick still answers with what the server held
      // before the PATCH committed, and its read stamp is legitimately current —
      // so the ordering fence cannot refuse it and the highlight would jump back
      // to the old Agent until the PATCH response arrives. The same is true of
      // any other producer of a row (a rename response, a find-or-create upsert,
      // a reconnect reconcile). Applied HERE rather than at each of them: the
      // callers are what keeps growing, the property is one. `confirmed` below is
      // deliberately untouched — a read still records what the server actually
      // holds, which is exactly the compare-and-set token the next write needs.
      const inFlight = latestProjectRouteRef.current;
      const resolved =
        requested && inFlight.size
          ? requested.map((project) =>
              inFlight.has(project.id)
                ? { ...project, default_agent: appliedRoute(inFlight.get(project.id)!) }
                : project,
            )
          : requested;
      projectsRef.current = resolved;
      if (options && 'confirmed' in options) {
        const confirmed = confirmedProjectAgentRef.current;
        if (options.confirmed === null) confirmed.clear();
        else {
          for (const project of options.confirmed ?? []) {
            confirmed.set(project.id, project.default_agent?.agent_id ?? null);
          }
        }
      }
      setProjects(resolved);
    },
    [],
  );

  // ── One question, asked immediately before every read request ────────────────
  // Every read this provider owns commits into a cache, and two things can make
  // that commit impossible or pointless before the response even arrives. There may
  // be no reader: a REVALIDATION is by definition a read the next activation would
  // redo, so with nothing rendering the tree it is dropped (a POPULATING read
  // produces what nothing else will and is exempt by declaration — see
  // ``fetchSessions``). Or there may be no address: an authorization change, an
  // archive, or a document that never loaded the tree leaves the project's window
  // with nowhere to land, which every read already detects by discarding its own
  // response.
  //
  // Both used to be answered somewhere other than the moment of asking — the first
  // at the call sites that triggered a read, the second after the request had
  // already been paid for — and each new trigger or each extra page then had to
  // remember. So it is ONE predicate, evaluated per REQUEST rather than per read: a
  // paging read asks it before every page, and a read that loops to retry asks it
  // again before the retry.
  const projectIsInTree = useCallback(
    (projectId: string) => projectsRef.current?.some((project) => project.id === projectId) ?? false,
    [],
  );

  const canIssueRead = useCallback(
    (kind: 'revalidation' | 'population', projectId?: string) => {
      if (kind === 'revalidation' && !isActive()) return false;
      // A tree-wide read is its own address: the bootstrap is what CREATES the list
      // a per-project read is checked against.
      return projectId === undefined || projectIsInTree(projectId);
    },
    [isActive, projectIsInTree],
  );

  const flushBootstrapReadIntent = useCallback(() => {
    if (bootstrapReadInFlightRef.current) return;
    const pendingFetch = fetchProjectsPendingRef.current;
    if (pendingFetch) {
      fetchProjectsPendingRef.current = null;
      // ``fetchProjects`` is a POPULATING read and therefore exempt from the gate
      // the reads apply to themselves (see ``fetchSessions``) — a window a write
      // just expanded must not stay empty. A retry queued because a mutation
      // invalidated the read in flight has no such window to fill once the last
      // reader has detached, because activation re-reads unconditionally. So the
      // one read that cannot state demand for itself states it here, where its
      // retry is spent: keeping it would issue a bootstrap on a route that
      // renders no project, and after an authorization change it would repopulate
      // the tree ``discardAuthorizedTree`` just dropped, since invalidating the
      // read in flight is itself what queues the retry.
      if (canIssueRead('revalidation')) {
        fetchProjectsRunnerRef.current(pendingFetch);
        return;
      }
    }
    if (projectTreePendingRef.current) {
      // Ungated on purpose: this retry runs ``reconcileProjectTree``, a
      // revalidation that declines itself while nothing reads the tree — and
      // takes the window-preserving path if demand returned in the meantime.
      projectTreePendingRef.current = false;
      projectTreeRunnerRef.current();
    }
  }, [canIssueRead]);

  const queueFetchProjectsIntent = useCallback((options?: { cache?: boolean }) => {
    const pending = fetchProjectsPendingRef.current;
    fetchProjectsPendingRef.current =
      options?.cache === false || pending?.cache === false
        ? { cache: false }
        : pending ?? options ?? {};
    queueMicrotask(flushBootstrapReadIntent);
  }, [flushBootstrapReadIntent]);

  const queueProjectTreeIntent = useCallback(() => {
    projectTreePendingRef.current = true;
    queueMicrotask(flushBootstrapReadIntent);
  }, [flushBootstrapReadIntent]);

  const acceptSessionMutation = useCallback((projectId?: string | null, sessionId?: string) => {
    const resources = ['projects-bootstrap'];
    if (projectId) resources.push(`project:${projectId}`);
    if (sessionId) resources.push(`project-session:${sessionId}`);
    readOwnershipRef.current.acceptMutation(resources);
  }, []);

  const acceptProjectsMutation = useCallback(() => {
    readOwnershipRef.current.acceptMutation('projects');
  }, []);

  const projectIdForSession = useCallback((sessionId: string): string | null => {
    const known = sessionProjectRef.current.get(sessionId);
    if (known) return known;
    return Object.entries(sessionsRef.current).find(([, state]) =>
      state.sessions?.some((session) => session.id === sessionId),
    )?.[0] ?? null;
  }, []);

  const applyProjectsSnapshot = useCallback((nextProjects: WorkbenchProject[]) => {
    const accessibleIds = new Set(nextProjects.map((project) => project.id));
    commitProjects(nextProjects, { confirmed: nextProjects });
    setSessions((prev) =>
      Object.fromEntries(
        Object.entries(prev).filter(([projectId]) => accessibleIds.has(projectId)),
      ),
    );
    setExpanded((prev) => new Set([...prev].filter((projectId) => accessibleIds.has(projectId))));
    setCreating((prev) => new Set([...prev].filter((projectId) => accessibleIds.has(projectId))));
    for (const projectId of [...pendingReconcileRef.current.keys()]) {
      if (!accessibleIds.has(projectId)) pendingReconcileRef.current.delete(projectId);
    }
  }, [commitProjects]);

  // A reconnect and an authorization change are not the same kind of signal. A
  // reconnect says "you may have missed events", so deferring it while nothing
  // reads the tree costs nothing — activation re-reads anyway. An authorization
  // change says "what you already hold may no longer be authorized", and that
  // cannot wait for a consumer: the cache outlives the gate, and every recovery
  // path here deliberately PRESERVES what it has (``fetchProjects`` keeps the old
  // list when the read fails), so a tree loaded before the change would render
  // revoked rows on the way back. Dropping it returns this provider to exactly
  // its pre-mount state — which is what every document that never read the tree
  // already has — so the next activation bootstraps as a fresh load.
  const discardAuthorizedTree = useCallback(() => {
    // Fence the reads already in flight first: a response that left the server
    // before the change must not repopulate the cache we are dropping.
    const cachedProjectIds = new Set([
      ...Object.keys(sessionsRef.current),
      ...(projectsRef.current ?? []).map((project) => project.id),
    ]);
    readOwnershipRef.current.acceptMutation([
      'projects',
      'projects-bootstrap',
      // A read is not the only producer of a cached row: a create commits one
      // too, and it is in flight across exactly the same window. This resource
      // is bumped ONLY here, so an unrelated rename or archive cannot refuse a
      // create that authorization never touched.
      'projects-authorization',
      ...[...cachedProjectIds].map((projectId) => `project:${projectId}`),
      ...[...sessionProjectRef.current.keys()].map((sessionId) => `project-session:${sessionId}`),
    ]);
    commitProjects(null, { confirmed: null });
    sessionsRef.current = {};
    expandedRef.current = new Set();
    sessionProjectRef.current.clear();
    pendingReconcileRef.current.clear();
    pendingCachedRowRefreshRef.current.clear();
    // Pre-mount state includes "never loaded": the next activation must take the
    // authoritative first-page bootstrap, not a reconcile onto a dropped window.
    treeInitialFetched.current = false;
    setProjectsError(null);
    setSessions({});
    setExpanded(new Set());
    setCreating(new Set());
  }, [commitProjects]);

  // ── An outstanding window width is a DEBT, not a queued read ────────────────
  // A foreground restore asks for one row more than is loaded, because the row it
  // restored ranks just past the window. That minimum is the only record of it:
  // every other path sizes a project's window from the rows already cached, so a
  // read carrying the minimum that is then dropped — by the demand gate, by a
  // navigation, by a mutation — takes the restored row with it, and no later
  // revalidation puts it back.
  //
  // So the minimum is cleared by the commit that SATISFIES it, never by the attempt
  // that carries it, and every read that sizes a window takes its target from
  // ``windowTarget``. A refused request leaves the debt outstanding, which is what
  // makes the next activation rebuild the width the restore asked for instead of
  // the width the cache happens to hold.
  //
  // That makes it a LEVEL, so it is also the wrong thing for the reconcile loop to
  // re-enter on: it says work is owed, never that another attempt could pay it, and
  // a read that keeps failing would keep re-reading the same unpayable debt. The
  // loop re-enters on the ordering fence instead — see the tail of ``reconcileSessions``.
  const queueReconcile = useCallback((projectId: string, minCount = 0) => {
    const pending = pendingReconcileRef.current.get(projectId) ?? 0;
    pendingReconcileRef.current.set(projectId, Math.max(pending, minCount));
  }, []);

  const windowTarget = useCallback(
    (projectId: string) =>
      Math.max(
        sessionsRef.current[projectId]?.sessions?.length ?? 0,
        pendingReconcileRef.current.get(projectId) ?? 0,
      ),
    [],
  );

  // Only a read that sized itself to the debt may settle it. ``fetchSessions``
  // deliberately does not: it pages, it does not widen, so the reconcile it hands
  // the project to afterwards is what pays.
  const settlePendingReconcile = useCallback(
    (projectId: string, committedCount: number, exhausted: boolean) => {
      const pending = pendingReconcileRef.current.get(projectId);
      if (pending === undefined) return;
      // A wider minimum queued while this read was in flight is a different debt,
      // and it outlives a window that was already sized for the smaller one.
      if (exhausted || pending <= committedCount) pendingReconcileRef.current.delete(projectId);
    },
    [],
  );

  const acceptProjectRows = useCallback(
    (read: WorkbenchSessionReadStamp, projectId: string, rows: WorkbenchSession[]) => {
      for (const session of rows) {
        readOwnershipRef.current.claimRead(read, `project-session:${session.id}`);
        sessionProjectRef.current.set(session.id, projectId);
      }
    },
    [],
  );

  const applyBootstrapSessions = useCallback((
    read: WorkbenchSessionReadStamp,
    pages: Record<string, { sessions: WorkbenchSession[]; next_before_id: string | null } | undefined>,
  ) => {
    for (const [projectId, page] of Object.entries(pages)) {
      if (page) acceptProjectRows(read, projectId, page.sessions);
    }
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
  }, [acceptProjectRows]);

  const fetchProjects = useCallback(async (options?: { cache?: boolean }) => {
    if (bootstrapReadInFlightRef.current) {
      queueFetchProjectsIntent(options);
      return;
    }
    bootstrapReadInFlightRef.current = true;
    const read = readOwnershipRef.current.beginRead(['projects', 'projects-bootstrap']);
    const retryAfterMutation = () => {
      if (readOwnershipRef.current.isCurrent(read, ['projects', 'projects-bootstrap'])) return;
      // Upgrade any already-pending cached fetch to the authoritative retry.
      queueFetchProjectsIntent({ cache: false });
    };
    try {
      const result = await api.getWorkbenchProjectsBootstrap({ cache: options?.cache });
      const projectsCurrent = readOwnershipRef.current.isCurrent(read, 'projects');
      if (projectsCurrent) applyProjectsSnapshot(result.projects);
      if (!readOwnershipRef.current.isCurrent(read, 'projects-bootstrap')) {
        retryAfterMutation();
        return;
      }
      const pages = Object.fromEntries(
        Object.entries(result.sessions ?? {}).filter(([projectId]) =>
          readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`]),
        ),
      );
      applyBootstrapSessions(read, pages);
      if (!projectsCurrent) {
        retryAfterMutation();
        return;
      }
      setProjectsError(null);
    } catch (err) {
      // Don't strand consumers on an empty-state for a transient failure — keep
      // any list we had and surface the error (mobile shows a retry).
      if (readOwnershipRef.current.isCurrent(read, ['projects', 'projects-bootstrap'])) {
        setProjectsError(errorMessage(err) ?? String(err));
      } else {
        retryAfterMutation();
      }
    } finally {
      bootstrapReadInFlightRef.current = false;
      flushBootstrapReadIntent();
    }
  }, [api, applyBootstrapSessions, applyProjectsSnapshot, flushBootstrapReadIntent, queueFetchProjectsIntent]);

  // (Re)connect reconcile: rebuild a project's ALREADY-paged-in window (not just
  // page 1) so a transient SSE reconnect / controller restart doesn't truncate an
  // expanded project back to the first page. Pages in chunks because the server
  // clamps the limit to 200 — a single large request would silently truncate
  // windows >200 rows. Silent (no loading flag) so visible rows don't flicker.
  const reconcileSessions = useCallback(
    async (projectId: string, opts?: { minCount?: number }) => {
      // The demand gate belongs to the READS, not to the events that trigger them.
      // Guarding the paths this provider happened to know about — activation, a
      // reconnect, a queued retry — left every other trigger of a request-backed
      // revalidation to remember it, and there are more triggers than guards:
      // session activity, a pin re-order, a status or turn-end row refresh, a
      // trailing reconcile from a failed bootstrap. Stated at the read it becomes a
      // property of the read instead, and a long-lived admin tab stops paging in a
      // window it never shows. This read asks it per REQUEST (``canIssueRead``,
      // below) rather than on the way in, because it is a LOOP: an entry gate that
      // has already passed keeps rebuilding a multi-page window across the
      // navigation that removed its last reader.
      // Only the width is carried here. Whether the in-flight read must run again
      // is the fence's answer, not this guard's: every trigger that can arrive
      // mid-read accepts its mutation first (``acceptSessionMutation`` fences
      // ``project:<id>``), so the read in flight is already stale and its own tail
      // re-enters. A second record of that decision would be a second owner.
      if (inFlightRef.current.has(projectId)) {
        queueReconcile(projectId, opts?.minCount ?? 0);
        return;
      }
      // Recorded before the first request, so a gate refusal on page one loses the
      // width no more than a refusal between pages does.
      if (opts?.minCount) queueReconcile(projectId, opts.minCount);
      while (true) {
        const targetCount = windowTarget(projectId);
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
            // Both loops pass through here: the next page of this window, and the
            // fresh read the outer retry starts after a mutation refused this one.
            if (!canIssueRead('revalidation', projectId)) return;
            const res = await api.listSessions({
              projectId,
              status: 'active',
              limit: SESSIONS_PAGE_SIZE,
              beforeId: before,
              cache: false,
            });
            if (!projectIsInTree(projectId)) return;
            if (!readOwnershipRef.current.isCurrent(read, `project:${projectId}`)) {
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
          if (!stale && readOwnershipRef.current.isCurrent(read, `project:${projectId}`)) {
            acceptProjectRows(read, projectId, acc);
            setSessions((prev) => ({
              ...prev,
              [projectId]: { sessions: acc, cursor: nextBeforeId, loading: false, loadingMore: false, error: false },
            }));
            settlePendingReconcile(projectId, acc.length, nextBeforeId === null);
          }
        } catch {
          stale = !readOwnershipRef.current.isCurrent(read, `project:${projectId}`);
          /* keep the current window on a current failed reconcile */
        } finally {
          inFlightRef.current.delete(projectId);
        }
        // Re-enter on evidence THIS pass produced, and nothing else: a mutation
        // arrived that its response can no longer describe. A failed request is not
        // that evidence — it says the attempt could not be made, so retrying it here
        // is an unbounded retry of whatever just failed. The width it could not pay
        // stays outstanding for the next activation or event to pay instead, which
        // is exactly what a debt is for. Testing the debt here is what would spin.
        if (!stale) return;
        // Carry the width forward as the debt itself: ``setSessions`` has not
        // rendered yet, so the next pass cannot read it back off the cache.
        queueReconcile(projectId, targetCount);
      }
    },
    [acceptProjectRows, api, canIssueRead, projectIsInTree, queueReconcile, settlePendingReconcile, windowTarget],
  );

  const reconcileProjectTree = useCallback(async function reconcileProjectTree() {
    // Revalidation: dropped while nothing reads the tree (see ``reconcileSessions``).
    // Also a loop — one bootstrap per window-size group — so the question is asked
    // before each of them rather than once on the way in.
    if (bootstrapReadInFlightRef.current) {
      queueProjectTreeIntent();
      return;
    }
    bootstrapReadInFlightRef.current = true;
    const retryAfterInvalidation = (read: WorkbenchSessionReadStamp) => {
      if (readOwnershipRef.current.isCurrent(read, ['projects', 'projects-bootstrap'])) return;
      queueProjectTreeIntent();
    };
    const bootstrapGroups = new Map<number, string[]>();
    const largeProjectIds: string[] = [];
    for (const [projectId, state] of Object.entries(sessionsRef.current)) {
      if (!state || state.sessions === null) continue;
      // The width to rebuild is the cached one, or the wider one an undelivered
      // restore is still owed — this is where a debt the demand gate declined to
      // pay while nothing read the tree is finally honoured.
      const loadedCount = windowTarget(projectId);
      if (inFlightRef.current.has(projectId)) {
        queueReconcile(projectId, loadedCount);
        continue;
      }
      if (loadedCount > RECONNECT_SESSIONS_PAGE_SIZE) {
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
        if (!canIssueRead('revalidation')) return;
        const result = await api.getWorkbenchProjectsBootstrap({ cache: false });
        if (readOwnershipRef.current.isCurrent(read, 'projects')) applyProjectsSnapshot(result.projects);
      } else {
        let nextProjects: WorkbenchProject[] | null = null;
        const pages: Record<string, { sessions: WorkbenchSession[]; next_before_id: string | null }> = {};
        for (const [limit, projectIds] of groups) {
          // A window-size group per request: leaving the workbench between two of
          // them stops the rest, rather than finishing a rebuild nobody reads.
          if (!canIssueRead('revalidation')) return;
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
        if (readOwnershipRef.current.isCurrent(read, 'projects') && nextProjects) {
          applyProjectsSnapshot(nextProjects);
        }
        const currentPages = Object.fromEntries(
          Object.entries(pages).filter(([projectId]) =>
            readOwnershipRef.current.isCurrent(read, ['projects-bootstrap', `project:${projectId}`]),
          ),
        );
        applyBootstrapSessions(read, currentPages);
        for (const [projectId, page] of Object.entries(currentPages)) {
          settlePendingReconcile(projectId, page.sessions.length, page.next_before_id === null);
        }
      }
      if (!readOwnershipRef.current.isCurrent(read, ['projects', 'projects-bootstrap'])) {
        retryAfterInvalidation(read);
        return;
      }
      if (readOwnershipRef.current.isCurrent(read, 'projects')) setProjectsError(null);
      for (const projectId of largeProjectIds) {
        void reconcileSessions(projectId);
      }
    } catch (err) {
      if (!readOwnershipRef.current.isCurrent(read, ['projects', 'projects-bootstrap'])) {
        retryAfterInvalidation(read);
        return;
      }
      if (readOwnershipRef.current.isCurrent(read, 'projects')) setProjectsError(errorMessage(err) ?? String(err));
      const projectIds = [...bootstrapGroups.values()].flat();
      for (const projectId of [...projectIds, ...largeProjectIds]) {
        void reconcileSessions(projectId);
      }
    } finally {
      bootstrapReadInFlightRef.current = false;
      flushBootstrapReadIntent();
    }
  }, [api, applyBootstrapSessions, applyProjectsSnapshot, canIssueRead, flushBootstrapReadIntent, queueProjectTreeIntent, queueReconcile, reconcileSessions, settlePendingReconcile, windowTarget]);

  fetchProjectsRunnerRef.current = (options) => void fetchProjects(options);
  projectTreeRunnerRef.current = () => void reconcileProjectTree();

  // Bootstrap on the first active consumer rather than on mount, and revalidate
  // whenever the tree goes from unread to read again — the same contract as
  // ``ShowPagesInventoryStore.activate()``. WHICH read that is depends on what is
  // already cached, because ``applyBootstrapSessions`` REPLACES each project's
  // window with page one: a user who paged past the first eight sessions, stepped
  // onto an admin route (detaching the last reader) and came back would watch
  // those rows disappear and have to page through them again.
  // ``reconcileProjectTree`` is the window-preserving read, and it is already the
  // resumption path for the other signal that can arrive at a loaded tree — a
  // reconnect — so a returning consumer takes it too. The first-page bootstrap is
  // reserved for a tree that has none of this to preserve.
  //
  // One owner for that choice, because activation is not the only caller: an
  // authorization change re-reads for an active consumer too, and it drops the
  // window first — so "which read" has to be answered from the cache as it is at
  // that moment, not from which event asked.
  const readTreeForActiveConsumer = useCallback(() => {
    if (!treeInitialFetched.current) {
      treeInitialFetched.current = true;
      void fetchProjects();
      return;
    }
    void reconcileProjectTree();
  }, [fetchProjects, reconcileProjectTree]);

  useEffect(() => {
    if (!active) return;
    readTreeForActiveConsumer();
  }, [active, readTreeForActiveConsumer]);

  const refreshCachedSessionRow = useCallback(async function refreshCachedSessionRow(sessionId: string) {
    // Revalidation: dropped while nothing reads the tree (see ``reconcileSessions``).
    // The binding this refreshes gates an action on a row nobody is rendering, and
    // the reconcile on the next activation re-reads the row anyway. Asked inside the
    // loop, because another event landing mid-flight makes this read go round again.
    if (cachedRowRefreshInFlightRef.current.has(sessionId)) {
      pendingCachedRowRefreshRef.current.add(sessionId);
      return;
    }
    cachedRowRefreshInFlightRef.current.add(sessionId);
    try {
      while (true) {
        pendingCachedRowRefreshRef.current.delete(sessionId);
        const projectId = Object.entries(sessionsRef.current).find(([, state]) =>
          state.sessions?.some((session) => session.id === sessionId && !session.native_session_id),
        )?.[0];
        if (!projectId) return;
        if (!canIssueRead('revalidation', projectId)) return;
        const resource = `project-session:${sessionId}`;
        const read = readOwnershipRef.current.beginRead(resource);
        let stale = false;
        try {
          const updated = await api.getSession(sessionId, { cache: false });
          stale = !readOwnershipRef.current.isCurrent(read, resource);
          if (!stale) {
            sessionProjectRef.current.set(updated.id, projectId);
            setSessions((prev) => patchSessionRow(prev, sessionId, () => updated));
            return;
          }
        } catch {
          stale = !readOwnershipRef.current.isCurrent(read, resource);
          if (!stale && !pendingCachedRowRefreshRef.current.has(sessionId)) return;
          /* current best-effort failures are recovered by reconnect reconcile */
        }
        if (!stale && !pendingCachedRowRefreshRef.current.has(sessionId)) return;
      }
    } finally {
      cachedRowRefreshInFlightRef.current.delete(sessionId);
      if (pendingCachedRowRefreshRef.current.delete(sessionId)) {
        queueMicrotask(() => void refreshCachedSessionRow(sessionId));
      }
    }
  }, [api, canIssueRead]);

  // Load the first page (append=false) or the next page (append=true) of a
  // project's sessions, with dedupe + per-project serialisation.
  //
  // Deliberately NOT demand-gated, unlike the reconciles above: this read POPULATES
  // a window, so nothing else will produce what it fetches. Its readers reach it
  // only while they render the tree (``toggleExpanded`` / ``loadMore`` /
  // ``reloadSessions``), and its remaining callers are writes that just created a
  // session or a project and expanded it — dropping those would leave the group the
  // user opened rendering empty until they collapsed it, which is a worse trade than
  // the one request. Same for ``fetchProjects``: the authoritative first load, whose
  // only droppable path is the queued retry ``flushBootstrapReadIntent`` already gates.
  //
  // Exempt from the DEMAND half only. A populating read still has to have somewhere
  // to land, and this one already knows the answer — the membership check below
  // discards every response that arrives for a project the tree does not hold. A
  // cold /chat/:id visit that forks a session reaches this with a tree it never
  // loaded, so that request was paid for and thrown away; asking first is the same
  // question, one round-trip earlier.
  const fetchSessions = useCallback(
    async function fetchSessions(projectId: string, opts?: { append?: boolean }) {
      const append = opts?.append ?? false;
      if (append && !sessionsRef.current[projectId]?.cursor) return; // nothing more to load
      if (!canIssueRead('population', projectId)) return;
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
      let retryAfterInvalidation = false;
      try {
        const res = await api.listSessions({ projectId, status: 'active', limit: SESSIONS_PAGE_SIZE, beforeId });
        if (!projectIsInTree(projectId)) return;
        const currentRead = readOwnershipRef.current.isCurrent(read, `project:${projectId}`);
        retryAfterInvalidation = !currentRead && readOwnershipRef.current.isLatestRead(read);
        if (currentRead) acceptProjectRows(read, projectId, res.sessions);
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
        const currentRead = readOwnershipRef.current.isCurrent(read, `project:${projectId}`);
        retryAfterInvalidation = !currentRead && readOwnershipRef.current.isLatestRead(read);
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
        if (retryAfterInvalidation) {
          queueMicrotask(() => {
            // The retry a refused response queues is a REVALIDATION wearing this
            // read's name: with no reader left there is no window to keep filled,
            // because activation re-reads unconditionally. Same asymmetry, and the
            // same place it is spent, as ``flushBootstrapReadIntent``.
            if (!canIssueRead('revalidation', projectId)) return;
            if (!inFlightRef.current.has(projectId)) void fetchSessions(projectId, opts);
          });
        } else if (pendingReconcileRef.current.has(projectId)) {
          // The debt stays in the map either way; the reconcile reads it back
          // through ``windowTarget`` instead of being handed a copy.
          void reconcileSessions(projectId);
        }
      }
    },
    [acceptProjectRows, api, canIssueRead, projectIsInTree, reconcileSessions],
  );

  // Keep the tree live: patch a row's status dot / title from SSE, and refetch
  // projects + every loaded project's window when the stream (re)opens (the
  // crash-recovery reset that runs server-side during a drop has no subscriber to
  // broadcast to, so listSessions is the authoritative source on reconnect).
  useEffect(() => {
    const disconnect = api.connectWorkbenchEvents({
      // A reconnect only has a tree to recover when one was being read; while no
      // consumer reads it, activation is what fetches a fresh one. That is the
      // read's own rule now (``reconcileSessions``), so this handler — like every
      // other trigger below — states the intent and lets the read decide.
      onConnected: () => {
        void reconcileProjectTree();
      },
      // An authorization change is invalidation rather than revalidation, and the
      // two answer different questions. Demand decides whether a replacement READ
      // is worth issuing; it never decides whether the cache the change voided is
      // dropped. Putting the drop in the no-consumer branch reads as "nothing is
      // watching, so let it go" when the actual reason is that these rows may no
      // longer be authorized — so with a consumer the tree merely revalidated, and
      // ``fetchProjects`` deliberately KEEPS the old list when a read fails.
      // Revoked projects would stay on screen for as long as the replacement took,
      // and indefinitely if it never landed. Drop at the edge, then re-read only
      // for a consumer — which, the window having gone with the cache, is the
      // authoritative first load rather than a reconcile of nothing.
      onAuthorizationChanged: () => {
        discardAuthorizedTree();
        if (isActive()) readTreeForActiveConsumer();
      },
      onSessionActivity: (data) => {
        const projectId =
          projectsRef.current?.find((project) => project.scope_id === data.scope_id)?.id ??
          projectIdForSession(data.session_id);
        if (projectId) sessionProjectRef.current.set(data.session_id, projectId);
        acceptSessionMutation(projectId, data.session_id);
        if (data.event === 'archived') {
          sessionProjectRef.current.delete(data.session_id);
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
            if (projectId) {
              const loaded = sessionsRef.current[projectId]?.sessions?.length ?? 0;
              void reconcileSessions(projectId, { minCount: loaded });
            }
          }
          return;
        }
        if (!REORDER_ACTIVITY_EVENTS.has(data.event)) return;
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
        acceptSessionMutation(projectIdForSession(session_id), session_id);
        setSessions((prev) =>
          patchSessionRow(prev, session_id, (s) => (s.agent_status === agent_status ? s : { ...s, agent_status })),
        );
        if (agent_status !== 'running') void refreshCachedSessionRow(session_id);
      },
      onTurnEnd: ({ session_id }) => {
        acceptSessionMutation(projectIdForSession(session_id), session_id);
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
    discardAuthorizedTree,
    isActive,
    projectIdForSession,
    readTreeForActiveConsumer,
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
        sessionProjectRef.current.set(session.id, projectId);
        acceptSessionMutation(projectId, session.id);
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
        acceptProjectsMutation();
        // Take ONLY the field this request changed. A mutation response is a
        // snapshot of the row as that request found it, so installing it whole
        // would drag an unrelated in-flight route pick back to the route the
        // rename saw — and recording it as ``confirmed`` would hand the
        // compare-and-set token a route the server has since replaced, making the
        // user's next pick a deterministic ``project_agent_conflict``. A rename is
        // not route truth, so it records none.
        commitProjects((prev) =>
          prev
            ? prev.map((p) => (p.id === projectId ? { ...p, display_name: updated.display_name } : p))
            : prev,
        );
      } catch (err) {
        console.error('[workbench] rename project failed', err);
      }
    },
    [acceptProjectsMutation, api, commitProjects],
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
        if (projectId) sessionProjectRef.current.set(session.id, projectId);
        acceptSessionMutation(projectId, session.id);
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

  const sendProjectRoute = useCallback(
    async (route: ProjectDefaultAgent, projectId: string): Promise<boolean> => {
      // The compare-and-set token is derived HERE, at send time, from the last
      // route the server confirmed — never from the cache, which is showing the
      // pick this very request is trying to install. That is what keeps a
      // rejected pick from poisoning the next one: expecting the route the server
      // refused would make every retry a deterministic conflict.
      const expectedAgentId = confirmedProjectAgentRef.current.get(projectId) ?? null;
      try {
        // Always send the full 5-field route: a complete set is coherent whether
        // the user picked an agent (all set) or cleared it (all null → default
        // dropped).
        const updated = await api.updateProject(projectId, {
          agent_id: route.agent_id,
          expected_agent_id: expectedAgentId,
          agent_name: route.agent_name,
          agent_variant: route.agent_variant,
          model: route.model,
          reasoning_effort: route.reasoning_effort,
        });
        acceptProjectsMutation();
        // Only the newest pick's response is still the truth: a pick made while
        // this request was in flight is already on screen, and installing this
        // answer would drag the row back to the route the user clicked past — the
        // lag this optimistic path exists to remove. The response is still
        // recorded as confirmed, because the server did take it: that is what the
        // follow-up write must expect to find.
        const superseded = route !== latestProjectRouteRef.current.get(projectId);
        commitProjects(
          (prev) =>
            prev && !superseded ? prev.map((p) => (p.id === projectId ? updated : p)) : prev,
          { confirmed: [updated] },
        );
        return true;
      } catch (err) {
        // apiFetch already surfaced the toast; the settle re-read below is what
        // puts the optimistic row back to what the server actually holds.
        console.error('[workbench] set project default agent failed', err);
        return false;
      }
    },
    [acceptProjectsMutation, api, commitProjects],
  );

  const { write: writeProjectRoute, isSaving: isSavingDefaultAgent } = useCoalescedWrite<ProjectDefaultAgent>(
    'project-route',
    sendProjectRoute,
    {
      onSettled: useCallback(
        (projectId: string, committed: boolean) => {
          latestProjectRouteRef.current.delete(projectId);
          const replaced = preBurstRouteRef.current.get(projectId) ?? null;
          const hadBase = preBurstRouteRef.current.delete(projectId);
          if (committed) return;
          // A rejected pick lives only in this cache, so the rollback is local:
          // put back the route the burst replaced. Synchronous and complete on its
          // own — the previous revision awaited a whole-tree re-read instead, which
          // is a read this provider may only be able to QUEUE (``fetchProjects``
          // records a trailing intent when one is already in flight), so the
          // refused route stayed on screen after the indicator had gone.
          if (hadBase) {
            commitProjects((prev) =>
              prev ? prev.map((p) => (p.id === projectId ? { ...p, default_agent: replaced } : p)) : prev,
            );
          }
          // Still re-read, unawaited: the base is the last route the server
          // CONFIRMED, so it is right for a rejected write — but a compare-and-set
          // conflict means someone else moved the route, and only a read can say
          // to what. The revert above is already on screen, so nothing waits for
          // it, and a pick made meanwhile fences it through
          // ``acceptProjectsMutation``.
          void fetchProjects({ cache: false });
        },
        [commitProjects, fetchProjects],
      ),
    },
  );

  const setProjectDefaultAgent = useCallback(
    (projectId: string, route: ProjectDefaultAgent) => {
      // The picker highlight is CONTROLLED by this cache, so the pick lands here
      // within the click and the request follows behind it. `acceptProjectsMutation`
      // stops a projects read that is already in flight from re-installing the
      // pre-pick route on top of it.
      acceptProjectsMutation();
      // Read the route this pick replaces BEFORE the commit below overwrites it:
      // on failure that is what the row goes back to, and it is available here
      // and nowhere later.
      const replaced = projectsRef.current?.find((p) => p.id === projectId)?.default_agent ?? null;
      latestProjectRouteRef.current.set(projectId, route);
      // ``commitProjects``, not ``setProjects``: the ref it keeps in step is what
      // every read and reconcile of this cache addresses, so an optimistic row
      // written past it would be invisible to them and lost on the next commit.
      commitProjects((prev) =>
        prev ? prev.map((p) => (p.id === projectId ? { ...p, default_agent: appliedRoute(route) } : p)) : prev,
      );
      // Only the pick that OPENS the burst records the base; the ones folded into
      // it are already looking at optimistic state.
      if (writeProjectRoute(projectId, route)) preBurstRouteRef.current.set(projectId, replaced);
    },
    [acceptProjectsMutation, commitProjects, writeProjectRoute],
  );

  const archiveProject = useCallback(
    async (projectId: string) => {
      try {
        await api.archiveProject(projectId);
        acceptProjectsMutation();
        acceptSessionMutation(projectId);
        commitProjects((prev) => (prev ? prev.filter((p) => p.id !== projectId) : prev));
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
    [acceptProjectsMutation, acceptSessionMutation, api, commitProjects],
  );

  const renameSession = useCallback(
    async (projectId: string, sessionId: string, title: string) => {
      // Empty string clears to "untitled" server-side. Patch from the REST
      // response so the row updates even if the session.activity SSE drops; the
      // broadcast then reconciles the same value. Throws on failure (caller's
      // inline editor catches it) so a failed rename leaves the old title.
      const updated = await api.updateSession(sessionId, { title });
      acceptSessionMutation(projectId, sessionId);
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
      acceptSessionMutation(projectId, sessionId);
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
      acceptSessionMutation(projectId, sessionId);
      sessionProjectRef.current.delete(sessionId);
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
      acceptProjectsMutation();
      // create_project is find-or-create by path: opening a tracked folder returns
      // the existing project, refreshed. Drop any stale copy, hoist to top, expand.
      // The route this snapshot carries can be older than a pick still in flight;
      // defending that is ``commitProjects``' job now, not this call site's. Only
      // the CONFIRMATION stays here: this is a mutation response, so it may not
      // record a token for a route whose write has not answered yet.
      const routeInFlight = latestProjectRouteRef.current.has(project.id);
      commitProjects(
        (prev) => (prev ? [project, ...prev.filter((p) => p.id !== project.id)] : [project]),
        routeInFlight ? undefined : { confirmed: [project] },
      );
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
    [acceptProjectsMutation, commitProjects, fetchSessions],
  );

  // ── A write is the OTHER producer of a cached row ───────────────────────────
  // ``discardAuthorizedTree`` fences the reads in flight, because a read is how
  // rows normally arrive. But a create carries a row too, over the same await,
  // and it was issued under the gate that just changed — so leaving the commit
  // at the call site put the one path that can SEED a dropped tree outside the
  // fence: the response lands, `prev` is null, and `[project]` re-creates the
  // cache from an authorization the document no longer has.
  //
  // Owning the request here is what makes the stamp unforgeable by a caller: the
  // epoch is taken before the request leaves and spent before the commit, and no
  // call site can hold a row without one. `null` is not a failure — the project
  // was created — it means this document must not paint it.
  const createProject = useCallback(
    async (payload: { folder_path: string; display_name?: string }): Promise<WorkbenchProject | null> => {
      const write = readOwnershipRef.current.beginRead('projects-authorization');
      const project = await api.createProject(payload);
      if (!readOwnershipRef.current.isMutationCurrent(write, 'projects-authorization')) return null;
      upsertProjectToTop(project);
      return project;
    },
    [api, upsertProjectToTop],
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
      activate,
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
      isSavingDefaultAgent,
      archiveProject,
      renameSession,
      setSessionPinned,
      archiveSession,
      createProject,
    }),
    [
      projects,
      projectsError,
      fetchProjects,
      activate,
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
      isSavingDefaultAgent,
      archiveProject,
      renameSession,
      setSessionPinned,
      archiveSession,
      createProject,
    ],
  );

  return <WorkbenchProjectsContext.Provider value={value}>{children}</WorkbenchProjectsContext.Provider>;
};
