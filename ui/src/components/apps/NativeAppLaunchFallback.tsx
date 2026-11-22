import { useEffect } from 'react';
import { APP_LAUNCH_EVENT, appBodyParams, readAppLaunchSnapshot } from '../../apps/appLaunchProtocol';
import { useStandaloneAppTab } from '../../context/StandaloneAppTabContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { isNativeAppWindow } from '../../lib/desktopShell';
import { useLatestRef } from '../../lib/useLatestRef';

// Native-originated deep links have no UI request waiter. On the main Runtime,
// native only publishes this snapshot after a terminal construction failure.
// Keep delivery deduplication for the document, including consumer remounts.
const delivered = new WeakMap<Document, Set<string>>();
export function NativeAppLaunchFallback() {
  const standalone = useStandaloneAppTab();
  const manager = useLatestRef(useWindowManager());
  useEffect(() => {
    if (standalone || isNativeAppWindow() || window.self !== window.top || window.__AVIBE_APP_LAUNCH_PROTOCOL__ !== 1) return;
    const seen = delivered.get(document) ?? new Set<string>();
    delivered.set(document, seen);
    const receive = (value: unknown) => {
      const snapshot = readAppLaunchSnapshot(value);
      if (!snapshot || snapshot.source !== 'deep-link' || snapshot.appId !== 'showpage' || seen.has(snapshot.requestId)) return;
      seen.add(snapshot.requestId);
      manager.current.openApp('showpage', { reuse: true, title: snapshot.title, params: appBodyParams(snapshot) });
    };
    const onLaunch = (event: Event) => receive((event as CustomEvent).detail);
    window.addEventListener(APP_LAUNCH_EVENT, onLaunch);
    receive(window.__AVIBE_APP_LAUNCH__);
    return () => window.removeEventListener(APP_LAUNCH_EVENT, onLaunch);
  }, [manager, standalone]);
  return null;
}
