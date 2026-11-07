// The routed pages, each loaded on demand so the first screen downloads and
// parses only what it draws (docs/plans/web-ui-first-load.md). The home (`/`)
// stays in the entry, and so does Search (a few kB): opening it focuses its
// field inside the tap, which is what lets a phone's keyboard come up.
//
// The page the app opens on starts loading with the entry (for a conversation,
// index.html already preloads it: scripts/firstScreenPreload.ts), and once the
// shell is up every other page is fetched in idle time, so moving between pages
// later does not wait on the network.
import { lazyPage } from './lib/lazyPage';
import { whenIdle } from './lib/chatPrefetch';
import { isNativeShell } from './lib/native';
import { isIosDevice, isStandalonePwa } from './lib/platform';
import { readLastPwaPath } from './lib/pwaRouteMemory';
import { DeferredMentionEditor } from './components/workbench/deferredMentionEditor';
import { DeferredInboxPreview } from './components/workbench/InboxHoverPopover';
import { DeferredMarkdown } from './components/ui/deferredMarkdown';

// The chat composer's editor arrives with the page, so the composer never waits on it.
export const ChatPage = lazyPage(() => Promise.all([
  import('./components/workbench/ChatPage'),
  DeferredMentionEditor.preload(),
]).then(([m]) => m.ChatPage));
export const InboxPage = lazyPage(() => import('./components/workbench/InboxPage').then((m) => m.InboxPage));
export const ProjectsPage = lazyPage(() => import('./components/workbench/ProjectsPage').then((m) => m.ProjectsPage));
export const AgentsPage = lazyPage(() => import('./components/workbench/AgentsPage').then((m) => m.AgentsPage));
export const SkillsPage = lazyPage(() => import('./components/workbench/SkillsPage').then((m) => m.SkillsPage));
export const HarnessPage = lazyPage(() => import('./components/workbench/HarnessPage').then((m) => m.HarnessPage));
export const VaultsPage = lazyPage(() => import('./components/workbench/VaultsPage').then((m) => m.VaultsPage));
export const Wizard = lazyPage(() => import('./components/Wizard').then((m) => m.Wizard));

// One chunk for all of Settings (settingsRoutePages.ts).
const settings = () => import('./components/settings/settingsRoutePages');

export const SettingsLayout = lazyPage(() => settings().then((m) => m.SettingsLayout));
export const SettingsGeneralPage = lazyPage(() => settings().then((m) => m.SettingsGeneralPage));
export const SettingsNotificationsPage = lazyPage(() => settings().then((m) => m.SettingsNotificationsPage));
export const SettingsShortcutsPage = lazyPage(() => settings().then((m) => m.SettingsShortcutsPage));
export const SettingsComputerUsePage = lazyPage(() => settings().then((m) => m.SettingsComputerUsePage));
export const SettingsServicePage = lazyPage(() => settings().then((m) => m.SettingsServicePage));
export const SettingsPlatformsPage = lazyPage(() => settings().then((m) => m.SettingsPlatformsPage));
export const ChannelList = lazyPage(() => settings().then((m) => m.ChannelList));
export const UserList = lazyPage(() => settings().then((m) => m.UserList));
export const RemoteAccessPage = lazyPage(() => settings().then((m) => m.RemoteAccessPage));
export const SettingsBackendsPage = lazyPage(() => settings().then((m) => m.SettingsBackendsPage));
export const SettingsOpencodeProviderPage = lazyPage(() => settings().then((m) => m.SettingsOpencodeProviderPage));
export const SettingsClaudeProviderPage = lazyPage(() => settings().then((m) => m.SettingsClaudeProviderPage));
export const SettingsCodexProviderPage = lazyPage(() => settings().then((m) => m.SettingsCodexProviderPage));
export const SettingsBackendPage = lazyPage(() => settings().then((m) => m.SettingsBackendPage));
export const SettingsDependenciesPage = lazyPage(() => settings().then((m) => m.SettingsDependenciesPage));
export const SettingsMessagingPage = lazyPage(() => settings().then((m) => m.SettingsMessagingPage));
export const SettingsDiagnosticsPage = lazyPage(() => settings().then((m) => m.SettingsDiagnosticsPage));
export const SettingsLogsPage = lazyPage(() => settings().then((m) => m.SettingsLogsPage));
export const PermissionsPage = lazyPage(() => settings().then((m) => m.PermissionsPage));

const EXACT_PAGES: Record<string, { preload: () => Promise<unknown> }> = {
  '/inbox': InboxPage,
  '/projects': ProjectsPage,
  '/agents': AgentsPage,
  '/skills': SkillsPage,
  '/harness': HarnessPage,
  '/vaults': VaultsPage,
  '/setup': Wizard,
};

function pageForPath(pathname: string): { preload: () => Promise<unknown> } | null {
  if (pathname.startsWith('/chat/')) return ChatPage;
  if (pathname === '/settings' || pathname.startsWith('/settings/')) return SettingsLayout;
  return EXACT_PAGES[pathname] ?? null;
}

const ignore = () => {};

/**
 * Start loading the page the app is opening on, alongside the entry. An
 * installed iOS app launches at `/` and then restores the page it was left on
 * (PwaRouteMemory), so that page is the one to fetch.
 */
export function preloadInitialRoute(): void {
  if (typeof window === 'undefined') return;
  let pathname = window.location.pathname;
  if (pathname === '/' && !isNativeShell() && isIosDevice() && isStandalonePwa()) {
    pathname = readLastPwaPath() ?? pathname;
  }
  pageForPath(pathname)?.preload().catch(ignore);
}

// The order a reader is likely to need them: a conversation from the home or
// the Inbox first (and the Markdown the sidebar's Inbox peek and an editor's
// preview draw, already downloaded with it), then the tabs, then Settings.
const PREFETCH_ORDER = [
  ChatPage,
  DeferredInboxPreview,
  DeferredMarkdown,
  InboxPage,
  ProjectsPage,
  AgentsPage,
  SkillsPage,
  HarnessPage,
  VaultsPage,
  SettingsLayout,
];

let prefetchStarted = false;

/**
 * Once per document, after the shell is up: fetch the other pages in idle time,
 * one at a time, while the page is visible. This is code the app downloaded up
 * front before it was split, and a page that has it can still be opened after
 * the connection drops, as before.
 */
export function prefetchRoutePages(): void {
  if (prefetchStarted) return;
  prefetchStarted = true;
  const queue = [...PREFETCH_ORDER];
  const next = () => {
    const page = queue.shift();
    if (!page) return;
    if (document.visibilityState !== 'visible') {
      queue.unshift(page);
      document.addEventListener('visibilitychange', () => whenIdle(next), { once: true });
      return;
    }
    page.preload().catch(ignore).finally(() => whenIdle(next));
  };
  whenIdle(next);
}
