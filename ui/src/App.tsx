import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Wizard } from './components/Wizard';
import { AppShell } from './components/AppShell';
import { ErrorBoundary } from './components/ui/error-boundary';
import { Workbench } from './components/Workbench';
import { InboxPage } from './components/workbench/InboxPage';
import { SearchPage } from './components/workbench/SearchPage';
import { AgentsPage } from './components/workbench/AgentsPage';
import { SkillsPage } from './components/workbench/SkillsPage';
import { HarnessPage } from './components/workbench/HarnessPage';
import { VaultsPage } from './components/workbench/VaultsPage';
import { ChatPage } from './components/workbench/ChatPage';
import { ProjectsPage } from './components/workbench/ProjectsPage';
import { MorePage } from './components/workbench/MorePage';
import { Dashboard } from './components/Dashboard';
import { ChannelList } from './components/steps/ChannelList';
import { UserList } from './components/steps/UserList';
import { ShowPagesPage } from './components/ShowPagesPage';
import { RemoteAccessPage } from './components/RemoteAccessPage';
import { SettingsDiagnosticsPage } from './components/settings/SettingsDiagnosticsPage';
import { SettingsBackendsPage } from './components/settings/SettingsBackendsPage';
import { SettingsDependenciesPage } from './components/settings/SettingsDependenciesPage';
import { SettingsClaudeProviderPage } from './components/settings/SettingsClaudeProviderPage';
import { SettingsCodexProviderPage } from './components/settings/SettingsCodexProviderPage';
import { SettingsOpencodeProviderPage } from './components/settings/SettingsOpencodeProviderPage';
import { SettingsLogsPage } from './components/settings/SettingsLogsPage';
import { SettingsMessagingPage } from './components/settings/SettingsMessagingPage';
import { SettingsPlatformsPage } from './components/settings/SettingsPlatformsPage';
import { SettingsServicePage } from './components/settings/SettingsServicePage';
import { StatusProvider } from './context/StatusContext';
import { ApiProvider, useApi, ApiError } from './context/ApiContext';
import { ToastProvider } from './context/ToastContext';
import { ThemeProvider } from './context/ThemeContext';
import { WorkbenchInboxProvider } from './context/WorkbenchInboxContext';
import { WorkbenchProjectsProvider } from './context/WorkbenchProjectsContext';
import { ComposerBridgeProvider } from './context/ComposerBridgeContext';
import { AgentationToggle } from './components/AgentationToggle';
import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

// Apps layer pages are lazy: they share their chunk with the windowed app bodies
// (registry.tsx) instead of being pulled into the main entry by these routes, so
// the file browser / xterm code loads only when an app actually opens.
const AppsFileBrowserPage = lazy(() =>
  import('./components/workbench/AppsFileBrowserPage').then((m) => ({ default: m.AppsFileBrowserPage })),
);
const AppsTerminalPage = lazy(() =>
  import('./components/workbench/AppsTerminalPage').then((m) => ({ default: m.AppsTerminalPage })),
);
import { hasConfiguredPlatformCredentials } from './lib/platforms';
import { applyAppTitle } from './lib/documentTitle';
import { useTranslation } from 'react-i18next';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './components/ui/card';
import { Button } from './components/ui/button';

// Paths that bypass the setup guard so the wizard and diagnostics can show
// logs / doctor output even before configuration is complete.
const LOGIN_CHECK_PATHS = new Set(['/admin/logs', '/admin/settings/diagnostics']);

const RemoteLoginRedirect = ({ target }: { target: string }) => {
    const { t } = useTranslation();

    useEffect(() => {
        window.location.assign(target);
    }, [target]);

    return <div className="min-h-screen flex items-center justify-center bg-bg text-text">{t('common.loading')}</div>;
};

// Server error codes (from the Web UI's enforce_remote_access_cookie guard)
// that mean the control UI is reachable but is refusing THIS request on
// policy grounds — a disallowed entry host, or broken remote-access config —
// NOT that the instance is unconfigured. They must surface as an explicit
// "access blocked" screen; routing them to the setup wizard (the old
// catch-all) is what made a host mismatch look like a fresh install.
//
// Reachability: host_mismatch (the common case — opening a raw LAN/Tailscale
// setup_host while the tunnel is on) makes /api/session report {remote:false},
// so it reaches this catch directly. For a Host that matches the public remote
// URL, an unauthenticated session is classified remote-login-required *before*
// /api/config is fetched, so the disabled / session_secret_missing codes are
// hit mainly when an already-authenticated session loses remote access. Telling
// those host-matching-but-disabled visitors apart from "just log in" would need
// /api/session to carry the block reason — tracked as a follow-up.
const ACCESS_BLOCKED_CODES = new Set<string>([
    'remote_access_host_mismatch',
    'remote_access_config_unavailable',
    'remote_access_public_url_invalid',
    'remote_access_disabled',
    'remote_access_session_secret_missing',
]);

// Return the blocking code when a failed config/session fetch is a recognized
// remote-access policy block, else null. A 401/403 also counts: the session
// probe said we were fine, yet the config endpoint still refused us — that is
// "no permission", not "needs setup".
const accessBlockedCode = (error: unknown): string | null => {
    if (error instanceof ApiError) {
        if (error.code && ACCESS_BLOCKED_CODES.has(error.code)) return error.code;
        if (error.status === 401 || error.status === 403) return error.code ?? 'forbidden';
    }
    return null;
};

// Shown when the server is up but refuses to open the control panel at this
// entry point. Tells the user which URL to use instead of stranding them in
// the setup wizard.
const AccessBlocked = ({ code }: { code: string | null }) => {
    const { t } = useTranslation();
    return (
        <main className="min-h-screen flex items-center justify-center bg-bg text-text p-4">
            <Card className="max-w-md w-full">
                <CardHeader>
                    <CardTitle>{t('accessBlocked.title')}</CardTitle>
                    <CardDescription>{t('accessBlocked.body')}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    <p className="text-sm text-muted">{t('accessBlocked.hint')}</p>
                    {code ? (
                        <p className="text-xs text-muted font-mono">{t('accessBlocked.codeLabel')}: {code}</p>
                    ) : null}
                    <Button onClick={() => window.location.reload()}>{t('accessBlocked.retry')}</Button>
                </CardContent>
            </Card>
        </main>
    );
};

type GuardStatus = 'loading' | 'ready' | 'needs-setup' | 'remote-login-required' | 'access-blocked';

const notificationClickPath = (value: unknown): string | null => {
    if (typeof value !== 'string' || !value.startsWith('/')) return null;
    try {
        const parsed = new URL(value, window.location.origin);
        if (parsed.origin !== window.location.origin) return null;
        return parsed.pathname + parsed.search + parsed.hash;
    } catch {
        return null;
    }
};

const WebPushNotificationNavigator = () => {
    const navigate = useNavigate();

    useEffect(() => {
        if (!('serviceWorker' in navigator)) return;

        const onMessage = (event: MessageEvent) => {
            const data = event.data;
            if (!data || typeof data !== 'object' || data.type !== 'vibe.notification-click') return;
            const path = notificationClickPath(data.url);
            if (path) navigate(path);
        };

        navigator.serviceWorker.addEventListener('message', onMessage);
        return () => navigator.serviceWorker.removeEventListener('message', onMessage);
    }, [navigate]);

    return null;
};

// Wrapper to check if setup is needed.
//
// Validation is global (auth session + setup state), so we only run it
// once per mount (plus when ``bypassSetupGuard`` flips for the logs /
// diagnostics escape hatches). Earlier versions re-ran on every URL
// change and reset the layout to a "Loading..." div while the two API
// calls round-tripped — that made every sidebar click feel like a full
// page reload because ``<AppShell>`` got unmounted and re-mounted.
const AuthGuard = ({ children }: { children: ReactNode }) => {
    const { getConfig, getAuthSession } = useApi();
    const { t } = useTranslation();
    const location = useLocation();
    const guardTarget = location.pathname + location.search;
    const [guardStatus, setGuardStatus] = useState<GuardStatus>('loading');
    const [blockedCode, setBlockedCode] = useState<string | null>(null);
    const bypassSetupGuard = LOGIN_CHECK_PATHS.has(location.pathname);
    // Re-validate only when crossing the setup boundary, not on every
    // route change. The wizard completes by saving config and navigating
    // off /setup; that pathname flip re-runs the effect so the stale
    // `needs-setup` status refreshes to `ready` instead of bouncing the
    // user straight back to /setup.
    const isSetupRoute = location.pathname === '/setup';
    const previousIsSetupRouteRef = useRef(isSetupRoute);

    useEffect(() => {
        previousIsSetupRouteRef.current = isSetupRoute;
    }, [isSetupRoute]);

    useEffect(() => {
        let cancelled = false;

        if (bypassSetupGuard) {
            return;
        }

        // Reset to loading while (re)validating. On the setup-boundary
        // re-run this prevents a one-frame bounce: a stale `needs-setup`
        // on a non-/setup route would otherwise redirect to /setup before
        // the fresh config resolves. Showing Loading for that single
        // transition is fine — it's the setup boundary, not every nav.
        setGuardStatus('loading');

        getAuthSession().then(session => {
            if (cancelled) return;
            if (session.remote && !session.authenticated) {
                setGuardStatus('remote-login-required');
                return null;
            }
            return getConfig().then(config => {
                if (cancelled) return;
                const setupState = config?.setup_state;
                const setupReady = typeof setupState?.needs_setup === 'boolean'
                    ? setupState.needs_setup === false
                    : hasConfiguredPlatformCredentials(config);
                setGuardStatus(!config || !config.mode || !setupReady ? 'needs-setup' : 'ready');
            });
        }).catch(async (error) => {
            if (cancelled) return;
            const session = await getAuthSession().catch(() => null);
            if (cancelled) return;
            if (session?.remote && !session.authenticated) {
                setGuardStatus('remote-login-required');
                return;
            }
            const blocked = accessBlockedCode(error);
            if (blocked) {
                // Server is up and the session probe was fine, but it refused
                // the config read on policy grounds (e.g. the entry host isn't
                // allowed while remote access is on). Say so explicitly instead
                // of bouncing the visitor to /setup as if nothing is configured.
                console.warn('[AuthGuard] config access blocked', error);
                setBlockedCode(blocked);
                setGuardStatus('access-blocked');
                return;
            }
            console.error('[AuthGuard] setup check failed', error);
            // If fetch fails for local/non-remote use (e.g. config doesn't exist),
            // setup is needed. Remote 401s are handled by the session branch above.
            setGuardStatus('needs-setup');
        });

        return () => {
            cancelled = true;
        };
        // ``isSetupRoute`` (not ``guardTarget``) is the only route signal
        // in deps: re-validate when entering/leaving /setup so wizard
        // completion clears the stale needs-setup status, while ordinary
        // sidebar navigation never re-runs (which would re-mount the
        // shell behind the Loading state).
    }, [bypassSetupGuard, isSetupRoute, getConfig, getAuthSession]);

    if (bypassSetupGuard) return children;
    if (guardStatus === 'loading') {
        return <div className="min-h-screen flex items-center justify-center bg-bg text-text">{t('common.loading')}</div>;
    }
    if (guardStatus === 'remote-login-required') {
        return <RemoteLoginRedirect target={guardTarget} />;
    }
    if (guardStatus === 'access-blocked') {
        return <AccessBlocked code={blockedCode} />;
    }
    if (guardStatus === 'needs-setup') {
        if (location.pathname === '/setup') return children;
        // A wizard finish navigates from /setup to / before the re-validation
        // effect can flip `guardStatus` to loading. Without this render-time
        // bridge, the stale setup-required state immediately redirects back to
        // /setup, so the first finish appears to "not take" even though the
        // config was already saved with setup_completed=true.
        if (previousIsSetupRouteRef.current) {
            return <div className="min-h-screen flex items-center justify-center bg-bg text-text">{t('common.loading')}</div>;
        }
        return <Navigate to="/setup" replace />;
    }
    return children;
};

// Brief fallback while a lazy Apps route chunk loads (the pages render their own
// loading state once mounted).
const AppsRouteFallback = () => {
  const { t } = useTranslation();
  return <div className="grid min-h-[40vh] place-items-center text-[12px] text-muted">{t('common.loading')}</div>;
};

// Sets the browser tab title to "Avibe - <name>" from config (configured
// instance name, else system hostname). Config is cached in ApiContext, so this
// mount-time fetch is deduplicated with the AuthGuard's own config read.
const DocumentTitle = () => {
  const { getConfig } = useApi();
  useEffect(() => {
    let cancelled = false;
    getConfig()
      .then((config) => {
        if (!cancelled) applyAppTitle(config);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [getConfig]);
  return null;
};

function AppRoutes() {
  return (
    <>
    <DocumentTitle />
    <WebPushNotificationNavigator />
    <Routes>
      <Route element={<AuthGuard><AppShell /></AuthGuard>}>
        <Route path="/setup" element={<Wizard />} />

        {/* Workbench mode — `/` is the canvas root, the five capability
            entries (Inbox + Agents/Skills/Harness/Vaults) live alongside
            it. Commit 02 ships sidebar + placeholder pages; the real
            module screens land in later commits. */}
        <Route path="/" element={<Workbench />} />
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/search" element={<SearchPage />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/skills" element={<SkillsPage />} />
        <Route path="/harness" element={<HarnessPage />} />
        <Route path="/vaults" element={<VaultsPage />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/more" element={<MorePage />} />
        {/* Apps layer — File Browser (Phase 1) + Terminal (Phase 2). The
            sidebar Apps launcher opens these; /apps lands on the file browser. */}
        <Route path="/apps" element={<Navigate to="/apps/files" replace />} />
        <Route
          path="/apps/files"
          element={
            <Suspense fallback={<AppsRouteFallback />}>
              <AppsFileBrowserPage />
            </Suspense>
          }
        />
        <Route
          path="/apps/terminal"
          element={
            <Suspense fallback={<AppsRouteFallback />}>
              <AppsTerminalPage />
            </Suspense>
          }
        />
        <Route path="/chat/:sessionId" element={<ChatPage />} />

        {/* Control Panel mode — existing pages moved under /admin/* */}
        <Route path="/admin" element={<Navigate to="/admin/dashboard" replace />} />
        <Route path="/admin/dashboard" element={<Dashboard />} />
        <Route path="/admin/remote-access" element={<RemoteAccessPage />} />
        <Route path="/admin/groups" element={<ChannelList isPage />} />
        <Route path="/admin/users" element={<UserList />} />
        <Route path="/admin/show-pages" element={<ShowPagesPage />} />
        <Route path="/admin/logs" element={<SettingsLogsPage standalone />} />
        {/* No client-side route at /admin/settings: Flask owns GET /settings as
            a JSON API. The Flask handler redirects browser-Accept hits to
            /admin/settings/service. */}
        <Route path="/admin/settings/service" element={<SettingsServicePage />} />
        <Route path="/admin/settings/platforms" element={<SettingsPlatformsPage />} />
        <Route path="/admin/settings/backends" element={<SettingsBackendsPage />} />
        <Route path="/admin/settings/backends/opencode" element={<SettingsOpencodeProviderPage />} />
        <Route path="/admin/settings/backends/claude" element={<SettingsClaudeProviderPage />} />
        <Route path="/admin/settings/backends/codex" element={<SettingsCodexProviderPage />} />
        <Route path="/admin/settings/dependencies" element={<SettingsDependenciesPage />} />
        <Route path="/admin/settings/messaging" element={<SettingsMessagingPage />} />
        <Route path="/admin/settings/diagnostics" element={<SettingsDiagnosticsPage />} />
        <Route path="/admin/settings/logs" element={<SettingsLogsPage />} />

        {/* Legacy redirects: old top-level paths → /admin/* equivalents.
            Bookmarked URLs and external links keep working without a server
            round-trip. */}
        <Route path="/dashboard" element={<Navigate to="/admin/dashboard" replace />} />
        <Route path="/groups" element={<Navigate to="/admin/groups" replace />} />
        <Route path="/channels" element={<Navigate to="/admin/groups" replace />} />
        <Route path="/users" element={<Navigate to="/admin/users" replace />} />
        <Route path="/logs" element={<Navigate to="/admin/logs" replace />} />
        {/* Exact /settings — the server used to redirect browser hits here
            to the settings UI, but that handler moved to /api/settings in
            the route migration. Keep the bookmark working client-side. */}
        <Route path="/settings" element={<Navigate to="/admin/settings/service" replace />} />
        <Route path="/settings/service" element={<Navigate to="/admin/settings/service" replace />} />
        <Route path="/settings/platforms" element={<Navigate to="/admin/settings/platforms" replace />} />
        <Route path="/settings/backends" element={<Navigate to="/admin/settings/backends" replace />} />
        <Route path="/settings/backends/opencode" element={<Navigate to="/admin/settings/backends/opencode" replace />} />
        <Route path="/settings/backends/claude" element={<Navigate to="/admin/settings/backends/claude" replace />} />
        <Route path="/settings/backends/codex" element={<Navigate to="/admin/settings/backends/codex" replace />} />
        <Route path="/settings/dependencies" element={<Navigate to="/admin/settings/dependencies" replace />} />
        <Route path="/settings/messaging" element={<Navigate to="/admin/settings/messaging" replace />} />
        <Route path="/settings/diagnostics" element={<Navigate to="/admin/settings/diagnostics" replace />} />
        <Route path="/settings/logs" element={<Navigate to="/admin/settings/logs" replace />} />
        <Route path="/remote-access" element={<Navigate to="/admin/remote-access" replace />} />
        <Route path="/doctor" element={<Navigate to="/admin/settings/diagnostics" replace />} />
        <Route path="/doctor/logs" element={<Navigate to="/admin/logs" replace />} />
      </Route>
    </Routes>
    </>
  );
}

function App() {
  return (
    // Outermost backstop — OUTSIDE every provider, so a crash in a provider itself is contained too
    // (the per-page and per-window boundaries handle granular containment inside). ThemeProvider sets
    // data-theme on <html>, so the fallback still picks up the theme even from out here.
    <ErrorBoundary variant="page">
    <ThemeProvider>
      <StatusProvider>
        <ToastProvider>
          <ApiProvider>
            <WorkbenchInboxProvider>
              <WorkbenchProjectsProvider>
                <ComposerBridgeProvider>
                  <BrowserRouter>
                    <AppRoutes />
                  </BrowserRouter>
                  <AgentationToggle />
                </ComposerBridgeProvider>
              </WorkbenchProjectsProvider>
            </WorkbenchInboxProvider>
          </ApiProvider>
        </ToastProvider>
      </StatusProvider>
    </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
