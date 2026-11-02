import { useEffect, useState, type ReactNode } from 'react';

/**
 * Renders `children(false)` first and `children(true)` once mounted. Some product dialogs stay
 * mounted and open later (the Model Hub route dialog gets its `selection` after mount); one that
 * mounts already open starts a read during StrictMode's remount and then drops it, so a frame
 * opens such a dialog the way the product does: closed first, open right after.
 */
export function AfterMount({ children }: { children: (mounted: boolean) => ReactNode }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    // After the commit, so StrictMode's remount of the children has already run.
    let live = true;
    queueMicrotask(() => {
      if (live) setMounted(true);
    });
    return () => {
      live = false;
    };
  }, []);
  return <>{children(mounted)}</>;
}
