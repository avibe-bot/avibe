import { useCallback, useContext, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWindowManager, WindowLaunchLifetimeContext, type OpenAppOptions } from '../context/WindowManagerContext';
import { useRouteSurfaceActive } from '../lib/routeSurfaceActivity';
import { useLatestRef } from '../lib/useLatestRef';
import { isDesktopViewport } from '../lib/useIsDesktop';
import { isNativeAppWindow } from '../lib/desktopShell';
import { openLinkInNewContext } from '../lib/pwaNavigation';
import { appTabHref, type AppLaunchIntent } from './appLaunch';
import { appBodyParams, requestNativeAppLaunch, validAppLaunchTarget, type AppLaunchSource, type AppLaunchTarget } from './appLaunchProtocol';
import { showAppRoutePath } from '../components/apps/mobileDock';

// All launch surfaces keep their animation geometry locally; only typed launch
// state crosses the native boundary. The latest manager retains its foreground
// gate when an async caller is now behind Settings.
export function useAppLaunch(source: AppLaunchSource) {
  const wm = useLatestRef(useWindowManager());
  const active = useLatestRef(useRouteSurfaceActive());
  const navigate = useNavigate();
  const ownerLifetime = useContext(WindowLaunchLifetimeContext);
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);
  return useCallback(async (target: AppLaunchTarget, mode: AppLaunchIntent = 'activate', options?: Pick<OpenAppOptions, 'bounds'>) => {
    const controller = ownerLifetime?.controller ?? lifetime.current;
    if (!controller || controller.signal.aborted || !lifetime.current || lifetime.current.signal.aborted) return 'stale';
    if (!validAppLaunchTarget(target)) return 'stale';
    if (mode === 'newTab') {
      const href = target.params && Object.values(target.params).some((value) => value !== undefined) ? null : appTabHref(target);
      if (href) {
        openLinkInNewContext(href, 'noopener,noreferrer');
        return 'opened';
      }
      mode = 'activate';
    }
    const request = { ...target, source, mode, newWindow: mode === 'newWindow', origin: window.location.origin, requestId: crypto.randomUUID() };
    // Retired route command surfaces still settle locally behind Settings. They
    // cannot ask the OS to focus an App on behalf of an inactive surface.
    const outcome = active.current && window.__AVIBE_APP_LAUNCH_PROTOCOL__ === 1 ? await requestNativeAppLaunch(request, controller.signal) : 'fallback';
    if (outcome !== 'fallback' || controller.signal.aborted) return outcome;
    const params = appBodyParams(request);
    if (!isDesktopViewport() && !isNativeAppWindow()) {
      navigate(target.appId === 'showpage' ? showAppRoutePath(target.sessionId ?? '') : `/apps/${target.appId}`, { state: params });
      return 'fallback';
    }
    const manager = wm.current;
    manager.openApp(target.appId, {
      ...options, reuse: mode === 'activate', title: target.title,
      // Retire previous launch intent, retaining app-owned buffers and restore state.
      params: { path: undefined, cwd: undefined, filename: undefined, name: undefined, mime: undefined, initialTab: undefined, mtime: undefined, newFileDir: undefined, rootDir: undefined, line: undefined, column: undefined, endColumn: undefined, ...params },
    });
    return 'fallback';
  }, [active, navigate, ownerLifetime, source, wm]);
}
