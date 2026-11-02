import { useMemo, type ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';

import { ApiProvider } from '@/context/ApiContext';
import { BotRosterProvider } from '@/context/BotRosterProvider';
import { BotsProvider } from '@/context/BotsProvider';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { StatusContext, type RuntimeStatus } from '@/context/StatusContext';
import { ThemeContext } from '@/context/ThemeContext';
import { ToastProvider } from '@/context/ToastProvider';
import { UnsavedChangesContext } from '@/context/unsavedChangesContext';
import { WindowManagerContext, type WindowManagerValue } from '@/context/WindowManagerContext';
import { WorkbenchInboxContext, type InboxState } from '@/context/WorkbenchInboxContext';
import { WorkbenchProjectsContext, type WorkbenchProjectsTree } from '@/context/WorkbenchProjectsContext';
import { unreadByCategory } from '@/lib/notificationPreferences';

import type { DesignTheme } from '../frame';

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
  const totalUnread = counts.reduce((sum, n) => sum + n, 0);
  const botSessions = new Set((fixtures.bots ?? []).map((b) => b.session_id));
  return inert<InboxState>({
    inboxSessions: fixtures.inboxSessions ?? [],
    unreadBySession,
    unreadLoaded: true,
    totalUnread,
    unreadByCategory: unreadByCategory(unreadBySession, botSessions),
    // Every notification switch on, as a new install has them.
    badgeTotal: totalUnread,
    unreadSessions: counts.filter((n) => n > 0).length,
    nextCursor: null,
    loading: false,
    loadingMore: false,
    activateFeed: () => () => {},
  });
}

function statusValue(fixtures: WorkbenchFixtures) {
  const status: RuntimeStatus = fixtures.status ?? {};
  return { status, health: true, refreshStatus: () => Promise.resolve(status), control: () => Promise.resolve(null) };
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
 *  real ones, reading the frame's API fixtures through the network stub (`apiResponses`); the Bots
 *  provider publishes its roster to the real roster bridge the Inbox sorts by. */
export function DesignShell({
  fixtures = {},
  route = '/',
  theme = 'light',
  children,
}: {
  fixtures?: WorkbenchFixtures;
  route?: string;
  /** The frame's theme, for components that read it rather than the CSS (a canvas that paints its own colors). */
  theme?: DesignTheme;
  children: ReactNode;
}) {
  const inbox = useMemo(() => inboxValue(fixtures), [fixtures]);
  const projects = useMemo(() => projectsValue(fixtures), [fixtures]);
  const status = useMemo(() => statusValue(fixtures), [fixtures]);
  const themeValue = useMemo(() => ({ mode: theme, resolvedTheme: theme, setMode: () => {}, cycleMode: () => {} }), [theme]);
  const authorization = useMemo(
    () => ({
      remote: false,
      instanceKind: fixtures.instanceKind ?? null,
      instanceRole: 'owner' as const,
      capabilities: fixtures.capabilities ?? owner,
      readerPrincipal: fixtures.readerPrincipal,
    }),
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
                    <BotRosterProvider>
                      <BotsProvider>
                        <StatusContext.Provider value={status}>
                          <ThemeContext.Provider value={themeValue}>{children}</ThemeContext.Provider>
                        </StatusContext.Provider>
                      </BotsProvider>
                    </BotRosterProvider>
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
