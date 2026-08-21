import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, MonitorX, PinOff } from 'lucide-react';

import { useApi } from '../../context/ApiContext';
import { useDock } from '../../context/DockContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { showPageEmbeddedPath, showPagePrivatePath } from '../../apps/showPageAvatar';
import { useIsDesktop } from '../../lib/useIsDesktop';
import { Button } from '../ui/button';
import { ShowPageAnnotateControl } from '../workbench/ShowPageAnnotateControl';
import { useShowPageAnnotation } from '../workbench/useShowPageAnnotation';

// The `/apps/show/:sessionId` route — a pinned Show Page opened as an app on the
// current surface. Desktop keeps windows (mirrors LibraryRoute): focus an
// existing Show Page window for this session, else open one, then hand back to
// the workbench canvas. Mobile has no window layer, so it renders the page
// full-screen inside the AppShell chrome, framing the authed /show/<id>/ surface
// with a back affordance — the same private, same-origin-trusted surface the
// desktop window uses. Opening only READS: a missing/archived page shows a
// friendly placeholder, never a dead frame or an auto-created page.

export const ShowPageRoute: React.FC = () => {
  const { sessionId = '' } = useParams();
  const isDesktop = useIsDesktop();
  const wm = useWindowManager();
  const navigate = useNavigate();
  const handledRef = useRef(false);

  // Desktop: open (or focus) the Show Page window for this session and hand back
  // to the canvas. Guarded so the effect runs once even as window state ticks.
  useEffect(() => {
    if (!isDesktop || handledRef.current || !sessionId) return;
    handledRef.current = true;
    const own = wm.windows.filter((w) => w.appId === 'showpage' && w.params?.sessionId === sessionId);
    const target = own.find((w) => !w.minimized) ?? own[0];
    if (target) {
      if (target.minimized) wm.restore(target.id);
      else wm.focus(target.id);
    } else {
      wm.openApp('showpage', { params: { sessionId } });
    }
    navigate('/', { replace: true });
  }, [isDesktop, sessionId, wm, navigate]);

  if (isDesktop) return null; // transient: the effect hands back to the workbench canvas
  // Key by sessionId: React Router keeps this route element mounted across param
  // changes, so remount on a new session to reset loading state + title — a fresh
  // fetch with no stale title or missing/archived frame from the previous page.
  return <MobileShowPage key={sessionId} sessionId={sessionId} />;
};

// Full-screen mobile body: it owns the viewport below the safe-area inset, so
// AppShell withdraws its brand header, page padding, and bottom navigation.
const MobileShowPage: React.FC<{ sessionId: string }> = ({ sessionId }) => {
  const { t } = useTranslation();
  const api = useApi();
  const navigate = useNavigate();
  const { unpin } = useDock();

  const [state, setState] = useState<'loading' | 'ready' | 'missing'>(sessionId ? 'loading' : 'missing');
  const [title, setTitle] = useState('');
  const [annotateOpen, setAnnotateOpen] = useState(false);

  // Read the session once (error-suppressed — a gone session must NOT toast) to
  // upgrade the header to the LIVE title and to detect missing/archived, exactly
  // like the desktop Show Page window body.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    api
      .getSession(sessionId, { cache: true, handleError: false })
      .then((session) => {
        if (cancelled) return;
        if (!session || typeof session.id !== 'string' || session.status === 'archived') {
          setState('missing');
          return;
        }
        setState('ready');
        const live = (session.title ?? '').trim();
        if (live) setTitle(live);
      })
      .catch(() => {
        if (!cancelled) setState('missing');
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, api]);

  const label = title || t('apps.showPage.label');
  const missing = !sessionId || state === 'missing';
  const src = missing ? null : showPageEmbeddedPath(showPagePrivatePath(sessionId));
  const {
    state: annotationState,
    setIframe,
    handleIframeLoad,
    enable: enableAnnotation,
    disable: disableAnnotation,
    setMode: setAnnotationMode,
  } = useShowPageAnnotation(src);

  return (
    <div className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-background pb-[env(safe-area-inset-bottom)] pt-[env(safe-area-inset-top)]">
      <header className="flex shrink-0 items-center gap-3 border-b border-border bg-surface/70 px-4 py-2.5 backdrop-blur">
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={() => navigate(-1)}
          aria-label={t('common.back')}
          className="size-7 shrink-0"
        >
          <ArrowLeft className="size-3.5" />
        </Button>
        <span className="min-w-0 flex-1 truncate text-[14px] font-semibold text-foreground">{label}</span>
        <ShowPageAnnotateControl
          state={annotationState}
          onEnable={enableAnnotation}
          onDisable={disableAnnotation}
          onSetMode={setAnnotationMode}
          onPopoverOpenChange={setAnnotateOpen}
        />
      </header>

      {missing ? (
        <div className="grid flex-1 place-items-center px-6 text-center">
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
                  navigate(-1);
                }}
                className="mt-1 inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[12.5px] font-medium text-foreground transition hover:bg-foreground/[0.05]"
              >
                <PinOff className="size-3.5" />
                {t('apps.showPage.unpin')}
              </button>
            )}
          </div>
        </div>
      ) : (
        // Sandbox copied verbatim from the desktop Show Page window / ChatPage: the
        // workbench Show Page frame is intentionally same-origin-trusted — not hardened.
        <iframe
          ref={setIframe}
          onLoad={handleIframeLoad}
          title={t('chat.showPage.title')}
          src={src ?? undefined}
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
          allow="clipboard-write"
          className={`min-h-0 w-full flex-1 border-0 bg-background${annotateOpen ? ' pointer-events-none' : ''}`}
        />
      )}
    </div>
  );
};
