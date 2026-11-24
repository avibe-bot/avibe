import { useEffect, useState } from 'react';

import type { LiveStreamVia } from '@/lib/liveStreamVia';
import { nativeDirectPathReported, onNativeDirectPreference, readNativeDirectPath, type NativeDirectPathSnapshot } from '@/lib/native';

// The path's class and round trip change with no event: a move between two addresses of one family
// (a public and a private IPv4 address) is not a change of the path, and a new measurement is none.
const DIRECT_PATH_REREAD_MS = 5_000;

/**
 * The App's own direct path, read for display; re-read when it changes, and, from an App that
 * reports its path, every few seconds while the page is shown.
 */
export function useNativeDirectPath(pageVia: LiveStreamVia): NativeDirectPathSnapshot | null {
  const [snapshot, setSnapshot] = useState<NativeDirectPathSnapshot | null>(null);
  useEffect(() => {
    let alive = true;
    let latest = 0;
    const read = () => {
      // Only the latest read is shown, whatever order the answers come back in.
      const mine = ++latest;
      void readNativeDirectPath().then((next) => {
        if (alive && mine === latest) setSnapshot(next);
      });
    };
    read();
    const stop = onNativeDirectPreference(read);
    const timer = nativeDirectPathReported()
      ? window.setInterval(() => {
        if (document.visibilityState === 'visible') read();
      }, DIRECT_PATH_REREAD_MS)
      : null;
    return () => {
      alive = false;
      stop();
      if (timer !== null) window.clearInterval(timer);
    };
  }, [pageVia]);
  return snapshot;
}
