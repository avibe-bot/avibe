import { useMemo, type ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';

import { ApiProvider } from '@/context/ApiContext';
import { BotsProvider } from '@/context/BotsProvider';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { ToastProvider } from '@/context/ToastProvider';
import { UnsavedChangesContext } from '@/context/unsavedChangesContext';
import { WindowManagerContext, type WindowManagerValue } from '@/context/WindowManagerContext';
import { WorkbenchInboxContext, type InboxState } from '@/context/WorkbenchInboxContext';
import { WorkbenchProjectsContext, type WorkbenchProjectsTree } from '@/context/WorkbenchProjectsContext';

import { owner, type WorkbenchFixtures } from './fixtures';

/** Any member a frame does not set is a no-op: functions resolve to null, nothing throws. */
function inert<T extends object>(known: Partial<T>): T {
  return new Proxy(known, {
    get(target, key) {
      if (key in target) return target[key as keyof typeof target];
      return () => Promise.resolve(null);
    },
  }) as T;
}

function inboxValue(fixtures: WorkbenchFixtures): InboxState {
  const unreadBySession = fixtures.unreadBySession ?? {};
  const counts = Object.values(unreadBySession);
  return inert<InboxState>({
    inboxSessions: fixtures.inboxSessions ?? [],
    unreadBySession,
    unreadLoaded: true,
    totalUnread: counts.reduce((sum, n) => sum + n, 0),
    unreadSessions: counts.filter((n) => n > 0).length,
    nextCursor: null,
    loading: false,
    loadingMore: false,
    activateFeed: () => () => {},
  });
}

function projectsValue(fixtures: WorkbenchFixtures): WorkbenchProjectsTree {
  const expanded = new Set(fixtures.expandedProjects ?? []);
  return inert<WorkbenchProjectsTree>({
    projects: fixtures.projects ?? [],
    projectsError: null,
    activate: () => () => {},
    sessionsOf: (projectId: string) => ({
      sessions: fixtures.sessionsByProject?.[projectId] ?? [],
      loading: false,
      loadingMore: false,
      cursor: null,
      error: false,
    }),
    expanded,
    isExpanded: (projectId: string) => expanded.has(projectId),
    toggleExpanded: () => {},
    loadMore: () => {},
    reloadSessions: () => {},
    creatingSession: () => false,
    isReorderingProjects: false,
    isSavingDefaultAgent: () => false,
  });
}

/** The providers the Workbench surfaces read, served from fixtures. The API and Bots providers are the
 *  real ones, reading the frame's API fixtures through the network stub (`apiResponses`). */
export function DesignShell({ fixtures = {}, route = '/', children }: { fixtures?: WorkbenchFixtures; route?: string; children: ReactNode }) {
  const inbox = useMemo(() => inboxValue(fixtures), [fixtures]);
  const projects = useMemo(() => projectsValue(fixtures), [fixtures]);
  const authorization = useMemo(
    () => ({ remote: false, instanceKind: null, instanceRole: 'owner' as const, capabilities: fixtures.capabilities ?? owner }),
    [fixtures],
  );
  return (
    <MemoryRouter initialEntries={[route]}>
      <ToastProvider>
        <ApiProvider>
          <InstanceAuthorizationContext.Provider value={authorization}>
            <UnsavedChangesContext.Provider value={{ setRegistration: () => {}, authorizeRouteAction: () => null }}>
              <WindowManagerContext.Provider value={inert<WindowManagerValue>({ windows: [], focusedId: null, gestureActive: false })}>
                <WorkbenchInboxContext.Provider value={inbox}>
                  <WorkbenchProjectsContext.Provider value={projects}>
                    <BotsProvider>{children}</BotsProvider>
                  </WorkbenchProjectsContext.Provider>
                </WorkbenchInboxContext.Provider>
              </WindowManagerContext.Provider>
            </UnsavedChangesContext.Provider>
          </InstanceAuthorizationContext.Provider>
        </ApiProvider>
      </ToastProvider>
    </MemoryRouter>
  );
}
