import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { Plus, SquareTerminal, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { useWindowState } from '../../context/WindowManagerContext';
import { apiFetch } from '../../lib/apiFetch';
import { acquireTerminalSlot, releaseTerminalSlot } from '../../lib/terminalSlots';
import { MAX_RESTORED_TERMINAL_TABS, WINDOW_RESTORE_PARAM } from '../../lib/workbenchPersistence';
import type { TerminalStatus } from './TerminalView';

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

type Tab = { key: number; slot: number | null; title?: string };

// The Terminal's persisted snapshot (via useWindowState): just the tab count + any custom titles.
// Windowed terminal sessions are ephemeral by design (slot-based ids, DELETEd on pagehide), so we
// deliberately do NOT persist/reattach session ids — restore re-opens the same number of tabs with
// their names, over fresh shells.
type TerminalRestore = { tabs: { title?: string }[] };

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
export const TerminalTabs: React.FC<{ windowed?: boolean; windowId?: string; params?: Record<string, unknown> }> = ({ windowed = false, windowId, params }) => {
  const { t } = useTranslation();
  const { getAuthSession } = useApi();
  const [identity, setIdentity] = useState<string | null | undefined>(undefined); // undefined = resolving
  const tabSeq = useRef(0);
  // Restore the tab layout (count + custom titles) on reload for a windowed terminal, over fresh
  // slots/shells — the ephemeral session ids are never persisted (a deliberate security property).
  const [tabs, setTabs] = useState<Tab[]>(() => {
    const restore = windowed ? (params?.[WINDOW_RESTORE_PARAM] as TerminalRestore | undefined) : undefined;
    if (restore && Array.isArray(restore.tabs) && restore.tabs.length > 0) {
      // Cap at the backend session capacity before acquiring slots, so a corrupt/oversized array
      // can't open a flood of shells the terminal service can't admit.
      return restore.tabs.slice(0, MAX_RESTORED_TERMINAL_TABS).map((rt) => ({
        key: ++tabSeq.current,
        slot: acquireTerminalSlot(),
        title: typeof rt?.title === 'string' ? rt.title : undefined,
      }));
    }
    return [{ key: ++tabSeq.current, slot: windowed ? acquireTerminalSlot() : null }];
  });
  const [active, setActive] = useState<number>(() => tabs[0]?.key ?? 0);
  // Whether sessions actually persist (tmux available) — reported by TerminalView's ready frame.
  // Drives the tab-bar badge so it never falsely promises persistence for a plain-shell fallback.
  const [persistent, setPersistent] = useState(false);
  // Per-tab connection status (reported by each TerminalView). The tab bar shows the ACTIVE tab's
  // status merged with the persistence badge into ONE chip — there's no standalone status row.
  const [statuses, setStatuses] = useState<Record<number, TerminalStatus>>({});
  // Inline tab rename: the tab being renamed + its draft label (double-click a tab to start).
  const [editing, setEditing] = useState<{ key: number; value: string } | null>(null);
  const commitRename = () => {
    setEditing((cur) => {
      if (cur) {
        const v = cur.value.trim();
        setTabs((ts) => ts.map((x) => (x.key === cur.key ? { ...x, title: v || undefined } : x)));
      }
      return null;
    });
  };

  // Contribute the tab layout (count + custom titles) to the persisted window layout. No-ops for the
  // non-windowed route terminal (windowId undefined), which keeps its own persistent session id.
  useWindowState(windowId, (): TerminalRestore => ({ tabs: tabs.map((tb) => ({ title: tb.title })) }));

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
    setStatuses((m) => {
      if (!(key in m)) return m;
      const next = { ...m };
      delete next[key];
      return next;
    });
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
      {/* Tab bar (iwYIX): tabs + ＋, combined status + persistence chip on the right. */}
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
              {editing && editing.key === tab.key ? (
                <span className="flex items-center gap-1.5">
                  <SquareTerminal className="size-3.5 shrink-0 text-mint" />
                  <input
                    autoFocus
                    value={editing.value}
                    onChange={(e) => setEditing({ key: tab.key, value: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitRename();
                      else if (e.key === 'Escape') setEditing(null);
                    }}
                    onBlur={commitRename}
                    className="w-20 bg-transparent text-[12px] text-foreground focus:outline-none"
                  />
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => setActive(tab.key)}
                  onDoubleClick={() => setEditing({ key: tab.key, value: tab.title ?? t('apps.terminal.tabTitle', { n: i + 1 }) })}
                  title={t('apps.terminal.renameHint')}
                  className="flex items-center gap-1.5"
                >
                  <SquareTerminal className="size-3.5 text-mint" />
                  {tab.title ?? t('apps.terminal.tabTitle', { n: i + 1 })}
                </button>
              )}
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
        {/* One chip merges connection status + persistence (Alex: "connected" must not take its
            own row). Connected + tmux-backed shows the persistence label with a green dot; else it
            shows the connection status. Terminating states also get a reconnect inside the body. */}
        {(() => {
          const st = statuses[active] ?? 'connecting';
          const persistentReady = persistent && st === 'ready';
          const dotClass =
            st === 'ready'
              ? 'bg-mint'
              : st === 'connecting'
                ? 'bg-amber-400'
                : st === 'closed' || st === 'error'
                  ? 'bg-destructive'
                  : 'bg-muted';
          return (
            <span
              className={clsx(
                'flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] font-medium',
                persistentReady ? 'border-mint/30 bg-mint/[0.08] text-mint' : 'border-border bg-surface text-muted',
              )}
            >
              <span className={clsx('size-1.5 rounded-full', dotClass)} />
              {persistentReady ? t('apps.terminal.persistent') : t(`apps.terminal.status.${st}`)}
            </span>
          );
        })()}
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
                  <TerminalView
                    sessionId={sid}
                    onPersistent={setPersistent}
                    onStatus={(s) => setStatuses((m) => (m[tab.key] === s ? m : { ...m, [tab.key]: s }))}
                  />
                </Suspense>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
