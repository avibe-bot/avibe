import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';

import { ApiProvider } from '@/context/ApiContext';
import { BotRosterProvider } from '@/context/BotRosterProvider';
import { BotsProvider } from '@/context/BotsProvider';
import { DockContext, type DockValue } from '@/context/DockContext';
import { ShowPageDragContext, type ShowPageDragValue } from '@/context/showPageDrag';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { StatusContext, type RuntimeStatus } from '@/context/StatusContext';
import { ThemeContext } from '@/context/ThemeContext';
import { ToastProvider } from '@/context/ToastProvider';
import { UnsavedChangesContext } from '@/context/unsavedChangesContext';
import { WindowManagerContext, type WindowManagerValue } from '@/context/WindowManagerContext';
import { WorkbenchInboxContext, type InboxState } from '@/context/WorkbenchInboxContext';
import { WorkbenchProjectsContext, type WorkbenchProjectsTree } from '@/context/WorkbenchProjectsContext';
import { unreadByCategory } from '@/lib/notificationPreferences';

import type { DesignTheme, FrameRoute } from '../frame';

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

/** The Bots' Sessions, which the Inbox files under Bots rather than Sessions. */
const botSessionIds = (fixtures: WorkbenchFixtures) => new Set((fixtures.bots ?? []).map((bot) => bot.session_id));

function inboxValue(fixtures: WorkbenchFixtures): InboxState {
  const unreadBySession = fixtures.unreadBySession ?? {};
  const counts = Object.values(unreadBySession);
  const totalUnread = counts.reduce((sum, n) => sum + n, 0);
  return inert<InboxState>({
    inboxSessions: fixtures.inboxSessions ?? [],
    unreadBySession,
    unreadLoaded: !fixtures.inboxLoading,
    totalUnread,
    unreadByCategory: unreadByCategory(unreadBySession, botSessionIds(fixtures)),
    // Every notification switch on, as a new install has them.
    badgeTotal: totalUnread,
    unreadSessions: counts.filter((n) => n > 0).length,
    nextCursor: null,
    feedStatus: fixtures.inboxLoading ? 'pending' : fixtures.inboxFailed ? 'failed' : 'loaded',
    loading: fixtures.inboxLoading ?? false,
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
  const loading = new Set(fixtures.sessionsLoading ?? []);
  const loadingMore = new Set(fixtures.sessionsLoadingMore ?? []);
  const more = new Set([...(fixtures.moreSessions ?? []), ...loadingMore]);
  return inert<WorkbenchProjectsTree>({
    projects: fixtures.projectsLoading ? null : fixtures.projects ?? [],
    projectsLoaded: !fixtures.projectsLoading,
    projectsError: null,
    activate: () => () => {},
    sessionsOf: (projectId: string) => ({
      sessions: loading.has(projectId) ? null : fixtures.sessionsByProject?.[projectId] ?? [],
      loading: loading.has(projectId),
      loadingMore: loadingMore.has(projectId),
      cursor: more.has(projectId) ? 'more' : null,
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

/** The frame's fixtures, for providers a frame places itself (`InboxStateProvider`). */
const FixturesContext = createContext<WorkbenchFixtures>({});

/** One of the frame's `inboxStates`: the Inbox context this shell builds for the frame, with that
 *  state's rows and counts, for a frame that stacks the Inbox before and after a change. */
export function InboxStateProvider({ state, children }: { state: string; children: ReactNode }) {
  const fixtures = useContext(FixturesContext);
  const value = useMemo(() => {
    const rows = fixtures.inboxStates?.[state];
    return inboxValue(rows ? { ...fixtures, ...rows } : fixtures);
  }, [fixtures, state]);
  return <WorkbenchInboxContext.Provider value={value}>{children}</WorkbenchInboxContext.Provider>;
}

// Nothing is docked or being dragged: the chat header's Show Page control reads these.
const dock = inert<DockValue>({ order: [], pins: [], isPinned: () => false, isDocked: () => false, pinFor: () => null });
const showPageDrag = inert<ShowPageDragValue>({ active: false });

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
  route?: FrameRoute;
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
      <FixturesContext.Provider value={fixtures}>
      <ToastProvider>
        <ApiProvider>
          <InstanceAuthorizationContext.Provider value={authorization}>
            <UnsavedChangesContext.Provider value={{ setRegistration: () => {}, authorizeRouteAction: () => null }}>
              <WindowManagerContext.Provider value={inert<WindowManagerValue>({ windows: [], focusedId: null, gestureActive: false })}>
                <WorkbenchInboxContext.Provider value={inbox}>
                  <WorkbenchProjectsContext.Provider value={projects}>
                    <DockContext.Provider value={dock}>
                      <ShowPageDragContext.Provider value={showPageDrag}>
                        <BotRosterProvider>
                          <BotsProvider>
                            <StatusContext.Provider value={status}>
                              <ThemeContext.Provider value={themeValue}>{children}</ThemeContext.Provider>
                            </StatusContext.Provider>
                          </BotsProvider>
                        </BotRosterProvider>
                      </ShowPageDragContext.Provider>
                    </DockContext.Provider>
                  </WorkbenchProjectsContext.Provider>
                </WorkbenchInboxContext.Provider>
              </WindowManagerContext.Provider>
            </UnsavedChangesContext.Provider>
          </InstanceAuthorizationContext.Provider>
        </ApiProvider>
      </ToastProvider>
      </FixturesContext.Provider>
    </MemoryRouter>
  );
}
