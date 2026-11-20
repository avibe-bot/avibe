import { useApi } from '../../context/ApiContext';
import { ShowPageAnnotationHost } from '../workbench/ShowPageAnnotationHost';
import { showPageWindowSource, showPageWindowStatusAfterRead, type ShowPageWindowStatus } from './showPageWindowState';
import { isNativeAppWindow } from '../../lib/desktopShell';
import { useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useLocation, useParams } from 'react-router-dom';
import { APP_REGISTRY, type AppId } from '../../apps/registry';
import { appBodyParams, requestNativeWindowClose } from '../../apps/appLaunchProtocol';
import { WindowManagerContext } from '../../context/WindowManagerContext';
import { useAppLaunchSnapshot } from '../../apps/useAppLaunchSnapshot';
import { useStandaloneAppTab } from '../../context/StandaloneAppTabContext';
import { AppsEditorPage } from '../workbench/AppsEditorPage';
import { NativeShowShareEntry } from './NativeShowShareEntry';

// Reuses the shipped App bodies inside the shell's transient manager. The body
// stays mounted under fallback windows, so its editor buffers/terminals survive.
export function StandaloneAppRoute({ appId, children }: { appId: AppId; children?: ReactNode }) {
  const standalone = useStandaloneAppTab();
  const snapshot = useAppLaunchSnapshot();
  const location = useLocation();
  const { sessionId } = useParams();
  const launch = snapshot?.appId === appId ? snapshot : null;
  const params = useMemo(() => launch ? appBodyParams(launch) : {
    ...(typeof location.state === 'object' && location.state ? location.state : {}),
    ...(sessionId ? { sessionId } : {}),
    launchRequestId: location.key,
  }, [launch, location.key, location.state, sessionId]);
  if ((!standalone || (!isNativeAppWindow() && (appId === 'files' || appId === 'terminal'))) && children) return children;
  if (appId === 'editor') return <AppsEditorPage launchParams={params} />;
  const definition = APP_REGISTRY[appId];
  const Body = definition.Component;
  if (appId === 'showpage') {
    const windowId = launch?.windowId ?? 'route-showpage';
    return <NativeWindowCloseScope windowId={windowId}><StandaloneShowBody key={sessionId} windowId={windowId} params={params} /></NativeWindowCloseScope>;
  }
  const windowId = launch?.windowId ?? `route-${appId}`;
  return <NativeWindowCloseScope windowId={windowId}><div className="h-full min-h-0 w-full overflow-hidden bg-surface" data-theme={definition.lockTheme}>
    <Body windowId={windowId} params={params} />
  </div></NativeWindowCloseScope>;
}

// A body here IS its native window, which the in-page manager does not own: a
// body closing its own window id gets the native window's ordinary close, whose
// unsaved-state question replaces the in-page confirm. Every other id, and every
// surface that is not a native App window, keeps the in-page manager.
function NativeWindowCloseScope({ windowId, children }: { windowId: string; children: ReactNode }) {
  const manager = useContext(WindowManagerContext);
  const value = useMemo(() => manager && isNativeAppWindow() ? {
    ...manager,
    confirmClose: (id: string) => id === windowId || manager.confirmClose(id),
    close: (id: string) => {
      if (id !== windowId || !requestNativeWindowClose()) manager.close(id);
    },
  } : manager, [manager, windowId]);
  return <WindowManagerContext.Provider value={value}>{children}</WindowManagerContext.Provider>;
}

function StandaloneShowBody({ windowId, params }: { windowId: string; params: Record<string, unknown> }) {
  const api = useApi();
  const sessionId = typeof params.sessionId === 'string' ? params.sessionId : '';
  const [status, setStatus] = useState<ShowPageWindowStatus>('loading');
  const [archived, setArchived] = useState(false);
  const [revalidateVersion, setRevalidateVersion] = useState(0);
  // The same lifecycle source as the Workbench's Show windows: archive is
  // terminal for the frame and its controls, and every (re)connect re-reads
  // the session to close the gap the event stream could not cover.
  useEffect(() => api.connectWorkbenchEvents({
    onConnected: () => setRevalidateVersion((version) => version + 1),
    onSessionActivity: (data) => {
      if (data.event === 'archived' && data.session_id === sessionId) setArchived(true);
    },
  }), [api, sessionId]);
  useEffect(() => {
    let live = true;
    void api.getSessionResult(sessionId).then((result) => {
      if (live) setStatus((current) => showPageWindowStatusAfterRead(current, result));
    }).catch(() => { if (live) setStatus((current) => showPageWindowStatusAfterRead(current, { status: null, session: null })); });
    return () => { live = false; };
  }, [api, revalidateVersion, sessionId]);
  const Body = APP_REGISTRY.showpage.Component;
  // A native Show window has no web title bar: its share entry is the shell's title-bar button.
  const native = isNativeAppWindow();
  return <ShowPageAnnotationHost src={showPageWindowSource(sessionId, status, archived)}>
    {native ? <div className="relative h-full w-full">
      <Body windowId={windowId} params={params} />
      <NativeShowShareEntry sessionId={sessionId} />
    </div> : <Body windowId={windowId} params={params} />}
  </ShowPageAnnotationHost>;
}
