import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from '../../context/ApiContext';
import { apiFetch } from '../../lib/apiFetch';
import { acquireTerminalSlot, releaseTerminalSlot } from '../../lib/terminalSlots';

// Lazy so xterm.js stays out of the main bundle until the Terminal is opened.
const TerminalView = lazy(() => import('./TerminalView').then((m) => ({ default: m.TerminalView })));

// A process-unique in-memory fallback id, generated once per page load. Used only when
// localStorage is unavailable, so privacy-restricted/embedded browsers don't all collapse
// onto one shared tmux session (which would expose terminal state/commands across clients).
const FALLBACK_SESSION_ID = `wb-${Math.random().toString(36).slice(2, 10)}`;

// A per-tab token mixed into WINDOWED terminal session ids. The slot pool is module-local
// (so it's per browser tab), but the localStorage base id is shared across tabs in the same
// profile — so two tabs would both resolve their first windowed terminal to `<base>-w0` and
// fight over (reconnect/replace/DELETE) one backend session. This discriminator keeps each
// tab's windowed terminals distinct. The route terminal is intentionally shared/persistent
// across tabs (its own shell), so it does NOT use this.
const TAB_TOKEN = Math.random().toString(36).slice(2, 8);

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
// A windowed terminal also takes a bounded, reused session slot so each window gets
// its own backend session without minting unbounded session ids.
export const AppsTerminalPage: React.FC<{ windowed?: boolean }> = ({ windowed = false }) => {
  const { t } = useTranslation();
  const { getAuthSession } = useApi();
  // Resolve the signed-in identity first, then derive the (account-scoped) session id, so we
  // never briefly mount the terminal under the wrong key. email is null for local/unauth.
  const [sessionId, setSessionId] = useState<string | null>(null);
  const resolvedSessionIdRef = useRef<string | null>(null);
  const slotRef = useRef<number | null>(null);

  // Slot + backend-session lifecycle is tied to MOUNT, not to identity: acquire a bounded
  // slot for a windowed terminal on mount (so open/close churn reuses session ids instead
  // of exhausting the backend cap), and dispose the backend session + release the slot only
  // on unmount = window close. Keeping this OUT of the identity effect below is the point:
  // an ApiContext re-render hands `getAuthSession` a fresh identity, and if disposal lived
  // in that effect's cleanup it would DELETE a live terminal on every such refresh.
  useEffect(() => {
    if (!windowed) return;
    const slot = acquireTerminalSlot();
    slotRef.current = slot;
    return () => {
      slotRef.current = null;
      const sid = resolvedSessionIdRef.current;
      if (sid) {
        // Only return the slot to the pool once the backend session is actually GONE.
        // apiFetch resolves on non-2xx and network errors are swallowed, but a failed
        // DELETE (auth/origin/CSRF/server error) means the session may still be alive
        // (only detached via the WS close) — reusing the slot would then mint the same id
        // and reconnect a new window to the old shell, leaking its commands/state. 404 =
        // already gone, so that's safe to release too. On any other failure keep the slot
        // reserved (lost for this page, but the pool just hands out the next free index).
        void apiFetch(`/api/terminal/${encodeURIComponent(sid)}`, { method: 'DELETE', credentials: 'same-origin' })
          .then((res) => {
            if (res.ok || res.status === 404) releaseTerminalSlot(slot);
          })
          .catch(() => undefined);
      } else {
        releaseTerminalSlot(slot);
      }
    };
  }, [windowed]);

  // Resolve the (account-scoped) session id from the signed-in identity. May re-run when the
  // identity source changes; it only updates the id — it never disposes a session (that's the
  // mount-scoped effect above) — so a context refresh can't kill a live terminal.
  useEffect(() => {
    let cancelled = false;
    const resolve = (identity: string | null) => {
      if (cancelled) return;
      const slot = slotRef.current;
      const nextSessionId = getSessionId(identity, windowed && slot != null ? `${TAB_TOKEN}-w${slot}` : undefined);
      resolvedSessionIdRef.current = nextSessionId;
      setSessionId(nextSessionId);
    };
    getAuthSession()
      // Prefer the stable OIDC subject; email can be absent or shared across subjects, which
      // would collide or fall back to the shared key. (Backend surfaces sub on /api/session.)
      .then((session) => resolve(session.remote && session.authenticated ? session.sub || session.email : null))
      .catch(() => resolve(null));
    return () => {
      cancelled = true;
    };
  }, [getAuthSession, windowed]);

  // The React-cleanup DELETE above frees the session on an in-app window close, but it
  // doesn't run on a tab close / refresh. Dispose on `pagehide` too, with `keepalive` so
  // the request survives the unload — otherwise the windowed (per-tab id) session can't be
  // reattached or deleted on the next load and lingers until the backend idle reaper.
  useEffect(() => {
    if (!windowed) return;
    const dispose = () => {
      const sid = resolvedSessionIdRef.current;
      if (!sid) return;
      void apiFetch(`/api/terminal/${encodeURIComponent(sid)}`, {
        method: 'DELETE',
        credentials: 'same-origin',
        keepalive: true,
      }).catch(() => undefined);
    };
    window.addEventListener('pagehide', dispose);
    return () => window.removeEventListener('pagehide', dispose);
  }, [windowed]);

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
