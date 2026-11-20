import { useApi } from '../../context/ApiContext';
import { ShowPageAnnotationHost } from '../workbench/ShowPageAnnotationHost';
import { showPageWindowSource, showPageWindowStatusAfterRead, type ShowPageWindowStatus } from './showPageWindowState';
import { isNativeAppWindow } from '../../lib/desktopShell';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useLocation, useParams } from 'react-router-dom';
import { APP_REGISTRY, type AppId } from '../../apps/registry';
import { appBodyParams } from '../../apps/appLaunchProtocol';
import { useAppLaunchSnapshot } from '../../apps/useAppLaunchSnapshot';
import { useStandaloneAppTab } from '../../context/StandaloneAppTabContext';
import { AppsEditorPage } from '../workbench/AppsEditorPage';

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
  if (appId === 'showpage') return <StandaloneShowBody key={sessionId} windowId={launch?.windowId ?? 'route-showpage'} params={params} />;
  return <div className="h-full min-h-0 w-full overflow-hidden bg-surface" data-theme={definition.lockTheme}>
    <Body windowId={launch?.windowId ?? `route-${appId}`} params={params} />
  </div>;
}

function StandaloneShowBody({ windowId, params }: { windowId: string; params: Record<string, unknown> }) {
  const api = useApi();
  const sessionId = typeof params.sessionId === 'string' ? params.sessionId : '';
  const [status, setStatus] = useState<ShowPageWindowStatus>('loading');
  useEffect(() => {
    let live = true;
    void api.getSessionResult(sessionId).then((result) => {
      if (live) setStatus((current) => showPageWindowStatusAfterRead(current, result));
    }).catch(() => { if (live) setStatus((current) => showPageWindowStatusAfterRead(current, { status: null, session: null })); });
    return () => { live = false; };
  }, [api, sessionId]);
  const Body = APP_REGISTRY.showpage.Component;
  return <ShowPageAnnotationHost src={showPageWindowSource(sessionId, status, false)}><Body windowId={windowId} params={params} /></ShowPageAnnotationHost>;
}
