import { useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { MonitorX, PinOff } from 'lucide-react';

import { inTextEntrySurface } from '../components/apps/windowChords';
import { useRequiredShowPageAnnotationHost } from '../components/workbench/ShowPageAnnotationHostContext';
import { useDock } from '../context/DockContext';
import { useWindowManager } from '../context/WindowManagerContext';

// A pinned Show Page opened as a workbench app. The body always frames the
// PRIVATE /show/<session_id>/ surface (authenticated workbench context, live
// HMR while the agent keeps building the page) — never the public /p/<share>/
// link, regardless of the page's visibility. Opening the app only READS: it
// never ensures/creates a page, so a session whose page is gone or archived
// gets a friendly placeholder, not a dead frame. The letter-avatar + accent
// helpers live in ./showPageAvatar so the Dock/registry can use them without
// pulling this (lazy-loaded) window body into the main bundle.

export const ShowPageApp: React.FC<{ windowId: string; params?: Record<string, unknown> }> = ({ windowId, params }) => {
  const { t } = useTranslation();
  const { close, confirmClose } = useWindowManager();
  const { unpin } = useDock();
  const { annotation, src } = useRequiredShowPageAnnotationHost();
  const { setIframe: setAnnotationIframe, handleIframeLoad } = annotation;
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const setIframe = useCallback(
    (iframe: HTMLIFrameElement | null) => {
      iframeRef.current = iframe;
      setAnnotationIframe(iframe);
    },
    [setAnnotationIframe],
  );

  const sessionId = typeof params?.sessionId === 'string' ? params.sessionId : '';

  // Bridge ⌥W (close window) into the same-origin Show Page iframe: a keydown
  // inside the iframe dispatches to ITS document and never bubbles to the parent
  // WindowLayer listener, so without this ⌥W could not close the window while the
  // user is interacting with the page content (Codex §7.1f review). Re-attach on
  // each (re)load; text-entry surfaces inside the page keep Option+W for char entry.
  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== 'KeyW' || !e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
      let active: Element | null = null;
      try {
        active = iframe.contentDocument?.activeElement ?? null;
      } catch {
        active = null;
      }
      if (inTextEntrySurface(active)) return;
      e.preventDefault();
      if (confirmClose(windowId)) close(windowId);
    };
    const attach = () => {
      try {
        // Capture phase (`true`) on the iframe's WINDOW — the EARLIEST target in the
        // event path (window → document → element). A page that installs its own
        // capture-phase keydown listener and calls stopPropagation() would run before
        // a document-level capture listener and still swallow ⌥W; the window capture
        // runs before that, so only the explicit text-entry exemption above can
        // suppress the close shortcut (Codex §7.1g review).
        iframe.contentWindow?.addEventListener('keydown', onKeyDown, true);
      } catch {
        // Cross-origin (should not happen for the same-origin /show/ surface).
      }
    };
    attach();
    iframe.addEventListener('load', attach);
    return () => {
      iframe.removeEventListener('load', attach);
      try {
        iframe.contentWindow?.removeEventListener('keydown', onKeyDown, true);
      } catch {
        // Document already torn down.
      }
    };
  }, [close, confirmClose, iframeRef, windowId]);

  if (!sessionId || !src) {
    return (
      <div className="grid h-full w-full place-items-center bg-surface px-6 text-center">
        <div className="flex max-w-[320px] flex-col items-center gap-3">
          <span className="grid size-12 place-items-center rounded-2xl border border-border bg-foreground/[0.03] text-muted">
            <MonitorX className="size-6" />
          </span>
          <div className="space-y-1">
            <div className="text-[14px] font-semibold text-foreground">{t('apps.showPage.missingTitle')}</div>
            <p className="text-[12.5px] leading-relaxed text-muted">{t('apps.showPage.missingBody')}</p>
          </div>
          {sessionId && (
            <button
              type="button"
              onClick={() => {
                void unpin(sessionId);
                close(windowId);
              }}
              className="mt-1 inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[12.5px] font-medium text-foreground transition hover:bg-foreground/[0.05]"
            >
              <PinOff className="size-3.5" />
              {t('apps.showPage.unpin')}
            </button>
          )}
        </div>
      </div>
    );
  }

  // Sandbox is copied verbatim from the ChatPage show-page iframe: the workbench
  // Show Page frame is intentionally same-origin-trusted (the page authenticates
  // with the workbench cookie and runs its own same-origin fetches / WebSocket);
  // per the standing product decision we do NOT harden it here.
  return (
    <iframe
      ref={setIframe}
      onLoad={handleIframeLoad}
      title={t('chat.showPage.title')}
      src={src}
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
      allow="clipboard-write"
      className="h-full w-full border-0 bg-background"
    />
  );
};
