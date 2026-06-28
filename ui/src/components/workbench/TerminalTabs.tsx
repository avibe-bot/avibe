import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { Plus, SquareTerminal, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { apiFetch } from '../../lib/apiFetch';
import { acquireTerminalSlot, releaseTerminalSlot } from '../../lib/terminalSlots';

// Lazy so xterm.js stays out of the main bundle until a terminal opens.
const TerminalView = lazy(() => import('./TerminalView').then((m) => ({ default: m.TerminalView })));

const FALLBACK_SESSION_ID = `wb-${Math.random().toString(36).slice(2, 10)}`;
// Per-tab token, so two browser tabs / windows don't collide on the same slot id.
const TAB_TOKEN = Math.random().toString(36).slice(2, 8);

function getSessionId(identity: string | null, key?: string): string {
  const KEY = identity ? `avibe.terminal.sessionId.${encodeURIComponent(identity)}` : 'avibe.terminal.sessionId';
  try {
    let id = window.localStorage.getItem(KEY);
    if (!id) {
      id = `wb-${Math.random().toString(36).slice(2, 10)}`;
      window.localStorage.setItem(KEY, id);
    }
    return key ? `${id}-${key}` : id;
  } catch {
    return key ? `${FALLBACK_SESSION_ID}-${key}` : FALLBACK_SESSION_ID;
  }
}

type Tab = { key: number; slot: number | null };

// DELETE a slot-backed terminal session, then release its slot ONLY on a confirmed teardown
// (HTTP ok or 404). If the delete failed (expired auth, CSRF/origin rejection, network error, 5xx)
// the tmux session may still be alive, so the slot stays reserved — otherwise the next tab could
// reuse the same `<base>-wN` id and reconnect to the previous shell, exposing its state.
function deleteTerminalSession(sid: string, slot: number, keepalive = false): void {
  apiFetch(`/api/terminal/${encodeURIComponent(sid)}`, { method: 'DELETE', credentials: 'same-origin', keepalive })
    .then((res) => {
      if (res.ok || res.status === 404) releaseTerminalSlot(slot);
    })
    .catch(() => undefined);
}

// A multi-tab terminal: a tab bar (each tab = its own backend session) over the lazy
// xterm view. Reusable — the standalone Terminal app and (later) the editor's integrated
// terminal both mount this. Design: design.pen `iwYIX`.
//   - windowed: every tab is an ephemeral, slot-bounded session, disposed on close.
//   - route (non-windowed): the FIRST tab keeps the persistent localStorage session (so it
//     reconnects across reloads); extra tabs are slot-bounded like the windowed ones.
export const TerminalTabs: React.FC<{ windowed?: boolean }> = ({ windowed = false }) => {
  const { t } = useTranslation();
  const { getAuthSession } = useApi();
  const [identity, setIdentity] = useState<string | null | undefined>(undefined); // undefined = resolving
  const tabSeq = useRef(0);
  const [tabs, setTabs] = useState<Tab[]>(() => [{ key: ++tabSeq.current, slot: windowed ? acquireTerminalSlot() : null }]);
  const [active, setActive] = useState<number>(() => tabs[0]?.key ?? 0);
  // Whether sessions actually persist (tmux available) — reported by TerminalView's ready frame.
  // Drives the tab-bar badge so it never falsely promises persistence for a plain-shell fallback.
  const [persistent, setPersistent] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getAuthSession()
      .then((s) => !cancelled && setIdentity(s.remote && s.authenticated ? s.sub || s.email : null))
      .catch(() => !cancelled && setIdentity(null));
    return () => {
      cancelled = true;
    };
  }, [getAuthSession]);

  const sessionIdFor = (tab: Tab): string | null =>
    identity === undefined ? null : getSessionId(identity, tab.slot != null ? `${TAB_TOKEN}-w${tab.slot}` : undefined);

  // Best-effort dispose of a slot-backed (ephemeral) session; persistent tabs are left alone.
  const disposeTab = (tab: Tab, keepalive = false) => {
    if (tab.slot == null) return;
    const sid = sessionIdFor(tab);
    // No session id yet (identity unresolved → TerminalView never mounted) → no backend session
    // exists, so the slot is safe to release now. Otherwise release only after DELETE confirms.
    if (sid == null) releaseTerminalSlot(tab.slot);
    else deleteTerminalSession(sid, tab.slot, keepalive);
  };

  const addTab = () => {
    const tab: Tab = { key: ++tabSeq.current, slot: acquireTerminalSlot() };
    setTabs((ts) => [...ts, tab]);
    setActive(tab.key);
  };
  const closeTab = (key: number) => {
    setTabs((ts) => {
      const tab = ts.find((x) => x.key === key);
      if (tab) disposeTab(tab);
      const rest = ts.filter((x) => x.key !== key);
      if (rest.length === 0) {
        const fresh: Tab = { key: ++tabSeq.current, slot: windowed ? acquireTerminalSlot() : null };
        setActive(fresh.key);
        return [fresh];
      }
      if (active === key) setActive(rest[rest.length - 1].key);
      return rest;
    });
  };

  // Dispose every ephemeral session on unmount (window close) + on tab/page unload, reading
  // the live tabs from a ref so the listeners don't churn as tabs change.
  const liveRef = useRef<{ tabs: Tab[]; resolve: (t: Tab) => string | null }>({ tabs, resolve: sessionIdFor });
  liveRef.current = { tabs, resolve: sessionIdFor };
  useEffect(() => {
    const onHide = () => {
      for (const tab of liveRef.current.tabs) {
        if (tab.slot == null) continue;
        const sid = liveRef.current.resolve(tab);
        if (sid) void apiFetch(`/api/terminal/${encodeURIComponent(sid)}`, { method: 'DELETE', credentials: 'same-origin', keepalive: true }).catch(() => undefined);
      }
    };
    window.addEventListener('pagehide', onHide);
    return () => {
      window.removeEventListener('pagehide', onHide);
      for (const tab of liveRef.current.tabs) {
        if (tab.slot == null) continue;
        const sid = liveRef.current.resolve(tab);
        if (sid == null) releaseTerminalSlot(tab.slot);
        else deleteTerminalSession(sid, tab.slot);
      }
    };
  }, []);

  const loading = <div className="grid h-full w-full place-items-center text-[12px] text-muted">{t('common.loading')}</div>;

  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-surface">
      {/* Tab bar (iwYIX): tabs + ＋, persistent badge on the right. */}
      <div className="flex items-center gap-1 border-b border-border bg-surface-2/70 px-2 py-1">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {tabs.map((tab, i) => (
            <div
              key={tab.key}
              className={clsx(
                'group/tab flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] transition',
                tab.key === active ? 'bg-surface text-foreground shadow-[inset_0_-2px_0_0_var(--mint)]' : 'text-muted hover:bg-foreground/[0.05]',
              )}
            >
              <button type="button" onClick={() => setActive(tab.key)} className="flex items-center gap-1.5">
                <SquareTerminal className="size-3.5 text-mint" />
                {t('apps.terminal.tabTitle', { n: i + 1 })}
              </button>
              <button
                type="button"
                onClick={() => closeTab(tab.key)}
                aria-label={t('common.close')}
                className="grid size-4 place-items-center rounded text-muted opacity-0 transition hover:bg-foreground/10 hover:text-foreground group-hover/tab:opacity-100"
              >
                <X className="size-3" strokeWidth={2.5} />
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={addTab}
            aria-label={t('apps.terminal.newTab')}
            title={t('apps.terminal.newTab')}
            className="grid size-6 shrink-0 place-items-center rounded-md text-muted transition hover:bg-foreground/[0.06] hover:text-foreground"
          >
            <Plus className="size-3.5" strokeWidth={2.5} />
          </button>
        </div>
        {persistent && (
          <span className="shrink-0 rounded-full border border-mint/30 bg-mint/[0.08] px-2 py-0.5 text-[10px] font-medium text-mint">
            {t('apps.terminal.persistent')}
          </span>
        )}
      </div>

      {/* Panels: all tabs stay mounted so switching preserves each shell; hidden ones use
          display:none, so showing one fires the TerminalView ResizeObserver → refit. */}
      <div className="relative min-h-0 flex-1">
        {tabs.map((tab) => {
          const sid = sessionIdFor(tab);
          return (
            <div key={tab.key} className={clsx('absolute inset-0', tab.key === active ? 'block' : 'hidden')}>
              {sid == null ? loading : (
                <Suspense fallback={loading}>
                  <TerminalView sessionId={sid} onPersistent={setPersistent} />
                </Suspense>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
