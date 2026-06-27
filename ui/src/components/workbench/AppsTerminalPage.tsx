import { Suspense, lazy, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from '../../context/ApiContext';

// Lazy so xterm.js stays out of the main bundle until the Terminal is opened.
const TerminalView = lazy(() => import('./TerminalView').then((m) => ({ default: m.TerminalView })));

// A process-unique in-memory fallback id, generated once per page load. Used only when
// localStorage is unavailable, so privacy-restricted/embedded browsers don't all collapse
// onto one shared tmux session (which would expose terminal state/commands across clients).
const FALLBACK_SESSION_ID = `wb-${Math.random().toString(36).slice(2, 10)}`;

// A stable per-browser session id so the tmux-backed session reconnects to the same shell
// after a refresh / network drop (persistence). Falls back to the in-memory id above when
// localStorage is unavailable. The key is scoped to the signed-in account so a different
// remote (OIDC) user in the same browser can't inherit — and reconnect to — the previous
// user's live shell; local/unauthenticated sessions (identity == null) share one key.
function getSessionId(identity: string | null, windowKey?: string): string {
  const KEY = identity ? `avibe.terminal.sessionId.${encodeURIComponent(identity)}` : 'avibe.terminal.sessionId';
  try {
    let id = window.localStorage.getItem(KEY);
    if (!id) {
      id = `wb-${Math.random().toString(36).slice(2, 10)}`;
      window.localStorage.setItem(KEY, id);
    }
    // A windowed terminal appends its (per-instance, life-stable) window id, so two
    // terminal windows — or a window and the /apps/terminal route — don't share one
    // backend session. The service evicts a session's previous client on attach, so a
    // shared id would make them disconnect each other.
    return windowKey ? `${id}-${windowKey}` : id;
  } catch {
    return windowKey ? `${FALLBACK_SESSION_ID}-${windowKey}` : FALLBACK_SESSION_ID;
  }
}

// `windowed` renders just the terminal filling its parent (an AppWindow body) — no
// page header / viewport-height wrapper, since the window chrome supplies the title.
// `windowKey` (the owning window id) makes each windowed terminal its own session.
export const AppsTerminalPage: React.FC<{ windowed?: boolean; windowKey?: string }> = ({
  windowed = false,
  windowKey,
}) => {
  const { t } = useTranslation();
  const { getAuthSession } = useApi();
  // Resolve the signed-in identity first, then derive the (account-scoped) session id, so we
  // never briefly mount the terminal under the wrong key. email is null for local/unauth.
  const [sessionId, setSessionId] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const resolve = (identity: string | null) => {
      if (!cancelled) setSessionId(getSessionId(identity, windowKey));
    };
    getAuthSession()
      // Prefer the stable OIDC subject; email can be absent or shared across subjects, which
      // would collide or fall back to the shared key. (Backend surfaces sub on /api/session.)
      .then((session) => resolve(session.remote && session.authenticated ? session.sub || session.email : null))
      .catch(() => resolve(null));
    return () => {
      cancelled = true;
    };
  }, [getAuthSession, windowKey]);

  const loading = <div className="grid h-full w-full place-items-center text-[12px] text-muted">{t('common.loading')}</div>;
  const content =
    sessionId == null ? (
      loading
    ) : (
      <Suspense fallback={loading}>
        <TerminalView sessionId={sessionId} />
      </Suspense>
    );

  if (windowed) {
    return <div className="h-full w-full overflow-hidden bg-surface">{content}</div>;
  }

  return (
    <div className="flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]">
      <div>
        <h1 className="text-[18px] font-semibold text-foreground">{t('apps.terminal.label')}</h1>
        <p className="text-[12px] text-muted">{t('apps.terminal.tagline')}</p>
      </div>
      <div className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-surface">{content}</div>
    </div>
  );
};
