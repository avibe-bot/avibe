import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MonitorX, PinOff } from 'lucide-react';

import { useApi } from '../context/ApiContext';
import { useDock } from '../context/DockContext';
import { useWindowManager } from '../context/WindowManagerContext';
import { showPagePrivatePath } from './showPageAvatar';

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
  const api = useApi();
  // Destructure the STABLE window-manager callbacks (useCallback-memoized) rather
  // than the whole context value: the value object changes identity on every
  // window focus/minimize/drag tick, and depending on it here would re-run this
  // "read once" effect and re-hit /api/sessions on every such change (Codex).
  const { setTitle, close } = useWindowManager();
  const { unpin } = useDock();

  const sessionId = typeof params?.sessionId === 'string' ? params.sessionId : '';
  // 'loading' optimistically frames the page (the common case: it exists);
  // 'missing' swaps to the placeholder once we learn the session is gone/archived.
  const [state, setState] = useState<'loading' | 'ready' | 'missing'>(sessionId ? 'loading' : 'missing');

  // Read the session once (error-suppressed — a gone session must NOT toast) to
  // upgrade the window title to the LIVE title and to detect missing/archived.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    api
      .getSession(sessionId, { cache: true, handleError: false })
      .then((session) => {
        if (cancelled) return;
        // handleError:false resolves (does not throw) on a 404, returning the raw
        // error body — so a response without a real session id, or an archived
        // session, is the "missing" signal, same as a rejection.
        if (!session || typeof session.id !== 'string' || session.status === 'archived') {
          setState('missing');
          return;
        }
        setState('ready');
        const live = (session.title ?? '').trim();
        if (live) setTitle(windowId, live);
      })
      .catch(() => {
        if (!cancelled) setState('missing');
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, api, setTitle, windowId]);

  if (!sessionId || state === 'missing') {
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
      title={t('chat.showPage.title')}
      src={showPagePrivatePath(sessionId)}
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
      allow="clipboard-write"
      className="h-full w-full border-0 bg-background"
    />
  );
};
