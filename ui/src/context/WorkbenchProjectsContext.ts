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

export interface WorkbenchProjectsTree {
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
  /** Creates a session under a project (optimistic prepend + expand) and RETURNS it;
   *  the caller navigates (this provider is mounted outside the router). null on failure.
   *  `overrides` lets the create surfaces pin an agent/backend; omit for the server default. */
  createSessionForProject: (projectId: string, overrides?: Partial<WorkbenchSessionCreate>) => Promise<WorkbenchSession | null>;
  /** Fork an existing session, prepend the new row to the source project, and return it for navigation.
   *  `projectId` is a CACHE address, not a permission: a standalone session (`project_id: null`,
   *  i.e. no project-bound scope) forks with `null` and simply has no row to place. */
  forkSession: (projectId: string | null, sessionId: string) => Promise<WorkbenchSession | null>;
  renameProject: (projectId: string, name: string) => Promise<void>;
  /** Persist the project's default Agent route (Project Settings) and patch the
   *  shared cache so the sidebar + Projects page reflect it. Pass an all-null
   *  route to clear the default back to the global default. Throws on failure
   *  (the apiFetch layer already surfaced a toast) so the dialog can react. */
  setProjectDefaultAgent: (
    projectId: string,
    route: ProjectDefaultAgent,
    expectedAgentId: string | null,
  ) => Promise<void>;
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
  /** After NewProjectDialog: dedup-by-id, hoist to top, expand, fetch sessions if not loaded. */
  upsertProjectToTop: (project: WorkbenchProject) => void;
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
