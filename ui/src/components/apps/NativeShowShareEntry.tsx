import { useEffect, useRef, useState } from 'react';
import { useInRouterContext, useLocation } from 'react-router-dom';

import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { useRequiredShowPageAnnotationHost } from '../workbench/ShowPageAnnotationHostContext';
import { ShowPageAnnotateControl } from '../workbench/ShowPageAnnotateControl';
import { ShowPageShareControl } from '../workbench/ShowPageShareControl';

/** What the shell's title-bar share button dispatches into the native Show window's document. */
export const NATIVE_SHOW_SHARE_OPEN_EVENT = 'avibe:show-share-open';
export const NATIVE_SHOW_ANNOTATE_OPEN_EVENT = 'avibe:show-annotate-open';

/**
 * The native Show window's popover entries (ui/design: show-pages/desktop-native-controls/group-*): the
 * shell's Annotate and Share buttons toggle their shipped popovers below the first and third
 * cells. Each closes the other picker before opening its own. Escape
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
  const { annotation } = useRequiredShowPageAnnotationHost();
  const annotateAnchor = useRef<HTMLDivElement>(null);
  const [annotateOpen, setAnnotateOpen] = useState(false);
  const anchor = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const route = `/apps/show/${sessionId}`;
    const toggle = (event: Event) => {
      let path = routerPath ?? window.location.pathname;
      try {
        path = decodeURIComponent(path);
      } catch {
        return;
      }
      if (path !== route) return;
      const annotating = event.type === NATIVE_SHOW_ANNOTATE_OPEN_EVENT;
      if (annotating && open) anchor.current?.querySelector<HTMLButtonElement>('button')?.click();
      if (!annotating && annotateOpen) annotateAnchor.current?.querySelector<HTMLButtonElement>('button')?.click();
      const target = annotating ? annotateAnchor : anchor;
      target.current?.querySelector<HTMLButtonElement>('button')?.click();
    };
    window.addEventListener(NATIVE_SHOW_SHARE_OPEN_EVENT, toggle);
    window.addEventListener(NATIVE_SHOW_ANNOTATE_OPEN_EVENT, toggle);
    return () => {
      window.removeEventListener(NATIVE_SHOW_SHARE_OPEN_EVENT, toggle);
      window.removeEventListener(NATIVE_SHOW_ANNOTATE_OPEN_EVENT, toggle);
    };
  }, [routerPath, sessionId, open, annotateOpen]);

  if (!capabilities.can_use_show_pages || !sessionId) return null;
  return (
    <>
      {open || annotateOpen ? <div aria-hidden data-share-shield className="absolute inset-0 z-20" /> : null}
      {/* 28pt cells, 4pt gaps and a 9pt trailing inset mirror AppKit's accessory. */}
      <div className="pointer-events-none absolute right-titlebar-inset -top-7 z-30 flex gap-1">
        <div ref={annotateAnchor} data-native-annotate-anchor className="grid size-7 place-items-center">
          <ShowPageAnnotateControl
            compact
            anchorOnly
            state={annotation.state}
            onEnable={annotation.enable}
            onDisable={annotation.disable}
            onSetMode={annotation.setMode}
            onPopoverOpenChange={setAnnotateOpen}
          />
        </div>
        <div className="size-7" />
        <div ref={anchor} data-native-share-anchor className="grid size-7 place-items-center">
          <ShowPageShareControl
            compact
            anchorOnly
            sessionId={sessionId}
            canManageInstance={capabilities.can_manage_instance}
            canPinToDock={capabilities.can_chat}
            onOpenChange={setOpen}
          />
        </div>
        <div className="size-7" />
      </div>
    </>
  );
}
