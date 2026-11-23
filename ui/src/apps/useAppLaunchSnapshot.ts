import { useEffect, useState } from 'react';
import { APP_LAUNCH_EVENT, readAppLaunchSnapshot, type AppLaunchSnapshot } from './appLaunchProtocol';
import { isNativeAppWindow } from '../lib/desktopShell';

// Launch state is presentation data. Native still owns the caller, origin and
// window identity. A reused body never changes its app/window ownership.
export function useAppLaunchSnapshot(): AppLaunchSnapshot | null {
  const [snapshot, setSnapshot] = useState(() => isNativeAppWindow() ? readAppLaunchSnapshot(window.__AVIBE_APP_LAUNCH__) : null);
  useEffect(() => {
    if (!isNativeAppWindow()) return;
    const seen = new Set<string>();
    const accept = (value: unknown) => {
      const next = readAppLaunchSnapshot(value);
      if (!next || seen.has(next.requestId)) return;
      seen.add(next.requestId);
      setSnapshot((previous) => previous && (previous.windowId !== next.windowId || previous.appId !== next.appId || previous.sessionId !== next.sessionId) ? previous : next);
    };
    const receive = (event: Event) => accept((event as CustomEvent).detail);
    window.addEventListener(APP_LAUNCH_EVENT, receive);
    // Subscribe before rereading: initialization and an early repeated launch
    // can straddle React's mount without losing either latest snapshot.
    accept(window.__AVIBE_APP_LAUNCH__);
    return () => window.removeEventListener(APP_LAUNCH_EVENT, receive);
  }, []);
  return snapshot;
}
