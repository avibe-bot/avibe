import { useEffect, useRef, useState } from 'react';
import { useInRouterContext, useLocation } from 'react-router-dom';

import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { ShowPageShareControl } from '../workbench/ShowPageShareControl';

/** What the shell's title-bar share button dispatches into the native Show window's document. */
export const NATIVE_SHOW_SHARE_OPEN_EVENT = 'avibe:show-share-open';

/**
 * The native Show window's share entry (ui/design: show-pages/desktop-native-entry/titlebar-*): the
 * shell draws a share button in the window's title bar, and each press opens the shipped share
 * popover from the content's top right corner, or closes it if it is open, as Safari's does. Escape
 * and a click outside close it too; while it is open, a shield takes clicks that would otherwise
 * land in the Show's frame, which the page never hears.
 *
 * The press is answered only while this page is on this Show's own route: after navigating
 * elsewhere it does nothing, and back on the route it works again. The shell checks WebKit's own URL
 * before dispatching, and its script checks `location.pathname`, so a `pushState` the page makes
 * without the router is caught there; an open popover may then stay until the next press or click,
 * and the share it starts is checked natively.
 *
 * The page's own script can dispatch the same event, as it can click any control of this
 * same-origin document: at most it opens this window's share popover for this window's Show.
 */
export function NativeShowShareEntry({ sessionId }: { sessionId: string }) {
  return useInRouterContext() ? <RoutedEntry sessionId={sessionId} /> : <Entry sessionId={sessionId} routerPath={null} />;
}

/** Inside the app's router, the router's location is the page's. */
function RoutedEntry({ sessionId }: { sessionId: string }) {
  return <Entry sessionId={sessionId} routerPath={useLocation().pathname} />;
}

function Entry({ sessionId, routerPath }: { sessionId: string; routerPath: string | null }) {
  const { capabilities } = useInstanceAuthorization();
  const anchor = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const route = `/apps/show/${sessionId}`;
    const toggle = () => {
      let path = routerPath ?? window.location.pathname;
      try {
        path = decodeURIComponent(path);
      } catch {
        return;
      }
      if (path !== route) return;
      anchor.current?.querySelector<HTMLButtonElement>('button')?.click();
    };
    window.addEventListener(NATIVE_SHOW_SHARE_OPEN_EVENT, toggle);
    return () => window.removeEventListener(NATIVE_SHOW_SHARE_OPEN_EVENT, toggle);
  }, [routerPath, sessionId]);

  if (!capabilities.can_use_show_pages || !sessionId) return null;
  return (
    <>
      {open ? <div aria-hidden data-share-shield className="absolute inset-0 z-20" /> : null}
      <div ref={anchor} data-native-share-anchor className="absolute right-2 top-0 z-30 size-6">
        <ShowPageShareControl
          compact
          anchorOnly
          sessionId={sessionId}
          canManageInstance={capabilities.can_manage_instance}
          canPinToDock={capabilities.can_chat}
          onOpenChange={setOpen}
        />
      </div>
    </>
  );
}
