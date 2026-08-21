import { createContext, useContext, useEffect } from 'react';

import type { ProjectDefaultAgent, WorkbenchProject, WorkbenchSession, WorkbenchSessionCreate } from './ApiContext';

// The projects/sessions tree contract and the context handle every consumer
// reads. `WorkbenchProjectsProvider.tsx` owns the cache, the SSE subscription
// and the writes; keeping the handle in its own component-free module lets the
// provider hot-reload on its own.
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

/** The WRITE half of the tree, split out so that reading it and writing it are
 *  different requests to make of this provider.
 *
 *  A surface can be permanently mounted and still only ever write: `ChatPage`
 *  mounts `useSessionActions` on every `/chat/:id` for fork / pin / archive, and
 *  a session row mounts it for its rename menu. None of them render a project,
 *  so activating the bootstrap for them would fetch a tree nobody displays —
 *  the same waste this PR removed from `/admin`, arriving through a consumer
 *  instead of through a route.
 *
 *  Typing the halves apart is what makes that structural rather than a boolean
 *  each new call site has to remember: a writer reaches for
 *  ``useWorkbenchProjectsActions`` and *cannot* read `projects`, and a reader
 *  cannot compile against it and must take the activating hook below. */
export interface WorkbenchProjectsActions {
  /** Creates a session under a project (optimistic prepend + expand) and RETURNS it;
   *  the caller navigates (this provider is mounted outside the router). null on failure.
   *  `overrides` lets the create surfaces pin an agent/backend; omit for the server default. */
  createSessionForProject: (projectId: string, overrides?: Partial<WorkbenchSessionCreate>) => Promise<WorkbenchSession | null>;
  /** Fork an existing session, prepend the new row to the source project, and return it for navigation.
   *  `projectId` is a CACHE address, not a permission: a standalone session (`project_id: null`,
   *  i.e. no project-bound scope) forks with `null` and simply has no row to place. */
  forkSession: (projectId: string | null, sessionId: string) => Promise<WorkbenchSession | null>;
  renameProject: (projectId: string, name: string) => Promise<void>;
  /** Set the project's default Agent route (Project Settings): patches the shared
   *  cache FIRST, so the sidebar + Projects page + the picker's own highlight move
   *  within the click, then persists behind it. Pass an all-null route to clear
   *  the default back to the global default. Never throws and never resolves — a
   *  pick made while the last one is in flight replaces it, a failure is already
   *  toasted by the apiFetch layer, and the cache is reconciled from the server on
   *  settle. The route's compare-and-set token is NOT a parameter: only the writer
   *  knows which route the server last confirmed, and a caller reading it off this
   *  optimistic cache would expect a route the server may have refused. */
  setProjectDefaultAgent: (projectId: string, route: ProjectDefaultAgent) => void;
  /** Whether this project's default Agent is mid-write — from the pick until the
   *  server route is reconciled. Per project: the writes are partitioned per
   *  project too, so one project's slow save neither blocks nor spins another's
   *  picker. */
  isSavingDefaultAgent: (projectId: string) => boolean;
  archiveProject: (projectId: string) => Promise<void>;
  /** Throws on failure so the row's inline editor can fall back; patches title on success. */
  renameSession: (projectId: string, sessionId: string, title: string) => Promise<void>;
  /** Persist pin state and keep the session in the project's pinned-first order.
   *  `null` project = standalone session: the write still happens, only the
   *  project-scoped re-ordering is skipped. */
  setSessionPinned: (projectId: string | null, sessionId: string, pinned: boolean) => Promise<void>;
  /** Permanently archive a session: calls the API (which reclaims its bound
   *  tasks/watches/runs) then drops the row from the tree. Throws on failure.
   *  `null` project = standalone session (nothing to drop from the tree). */
  archiveSession: (projectId: string | null, sessionId: string) => Promise<void>;
  /** Create a project AND place it in the shared tree (dedup-by-id, hoist to top,
   *  expand, fetch sessions), then return it so the caller can navigate/select.
   *
   *  The commit lives here rather than at the call site because a write is the
   *  other way a row reaches the cache: the request is stamped before it leaves
   *  and the response is refused if an authorization change landed meanwhile, so
   *  a create begun under the old gate cannot re-seed a tree that was dropped.
   *  `null` means exactly that — the project exists on the server, this document
   *  may no longer be allowed to show it, and the next activation will decide. */
  createProject: (payload: { folder_path: string; display_name?: string }) => Promise<WorkbenchProject | null>;
}

export interface WorkbenchProjectsTree extends WorkbenchProjectsActions {
  projects: WorkbenchProject[] | null;
  projectsError: string | null;
  refreshProjects: () => Promise<void>;
  /** Refcounted activation (see ``useConsumerActivation``). The provider is
   *  mounted above the router, so it only bootstraps the tree while a consumer
   *  that renders it is mounted — an admin route mounts none. Consumers get
   *  this for free from ``useWorkbenchProjectsTree``. */
  activate: () => () => void;

  sessionsOf: (projectId: string) => ProjectSessionsState;
  expanded: ReadonlySet<string>;
  isExpanded: (projectId: string) => boolean;
  toggleExpanded: (projectId: string) => void;
  loadMore: (projectId: string) => void;
  /** Re-fetch the first page (mobile retry button / programmatic reload). */
  reloadSessions: (projectId: string) => void;

  creatingSession: (projectId: string) => boolean;
}

export const WorkbenchProjectsContext = createContext<WorkbenchProjectsTree | null>(null);

/** Read the shared projects/sessions tree.
 *
 *  Reading it is what makes the provider fetch it: every consumer activates by
 *  default, so the tree loads for whoever renders it and for nobody else. Pass
 *  ``active: false`` from a surface that is permanently mounted but only reads
 *  the tree while open (``NewSessionSheet`` via ``useNewSession``) — otherwise
 *  its mere presence would re-eagerize the bootstrap on every route. */
export function useWorkbenchProjectsTree(options?: { active?: boolean }): WorkbenchProjectsTree {
  const ctx = useContext(WorkbenchProjectsContext);
  const active = options?.active ?? true;
  const activate = ctx?.activate;
  useEffect(() => {
    if (!active || !activate) return;
    return activate();
  }, [active, activate]);
  if (!ctx) throw new Error('useWorkbenchProjectsTree must be used within a WorkbenchProjectsProvider');
  return ctx;
}

/** Write to the shared tree without reading it.
 *
 *  Deliberately does NOT activate: a rename, fork, pin or archive needs the
 *  cache patched if it happens to be loaded, and needs nothing fetched if it is
 *  not. The writes themselves are unconditional — they patch whatever is
 *  cached — so a document that only mutates issues no bootstrap at all. */
export function useWorkbenchProjectsActions(): WorkbenchProjectsActions {
  const ctx = useContext(WorkbenchProjectsContext);
  if (!ctx) throw new Error('useWorkbenchProjectsActions must be used within a WorkbenchProjectsProvider');
  return ctx;
}
