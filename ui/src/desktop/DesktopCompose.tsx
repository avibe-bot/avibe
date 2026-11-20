import { useEffect, useState, type ComponentType } from 'react';
import { isDesktopShell, isNativeAppWindow } from '../lib/desktopShell';

/**
 * Installs the Ask Vibey receiver in a desktop shell's Workbench document; renders nothing.
 *
 * Only a desktop shell ever calls the receiver, so its code is fetched there and
 * nowhere else, off the first screen every browser downloads
 * (docs/plans/web-ui-first-load.md). Until it is here the document has no
 * receiver, which the shell already reads as "still loading" and tries again
 * (desktop/src-tauri/src/compose.rs); a fetch that fails leaves it that way
 * rather than failing the page.
 */
export function DesktopCompose() {
  const [Mount, setMount] = useState<ComponentType | null>(null);
  useEffect(() => {
    if (!isDesktopShell() || isNativeAppWindow()) return undefined;
    let alive = true;
    void import('./DesktopComposeMount').then(
      (module) => { if (alive) setMount(() => module.DesktopComposeMount); },
      () => {},
    );
    return () => { alive = false; };
  }, []);
  return Mount ? <Mount /> : null;
}
