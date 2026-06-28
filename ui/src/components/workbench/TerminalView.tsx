import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import clsx from 'clsx';
import { RotateCw } from 'lucide-react';

import { Button } from '../ui/button';

// xterm.js wired to the /api/terminal/{id} WebSocket. Protocol (locked with the
// backend): client sends raw stdin as BINARY frames and JSON control as TEXT
// frames ({type:"resize",cols,rows}); server sends PTY output as BINARY and
// {type:"ready"|"exit"} as TEXT. Lazy-loaded by AppsTerminalPage so xterm stays
// out of the main bundle.
type Status = 'connecting' | 'ready' | 'closed' | 'disabled' | 'error';

const ENC = new TextEncoder();
const MAX_BUSY_RETRIES = 3; // auto-retry a transient "busy" (1013) close this many times

// Accessory key bar for phones (their soft keyboards lack these). Each button sends the raw
// byte sequence the PTY expects; Ctrl is a sticky modifier. Labels go through i18n (the
// control sequences stay here).
const KEYS: { labelKey: string; seq?: string; ctrl?: boolean }[] = [
  { labelKey: 'apps.terminal.keys.esc', seq: '\x1b' },
  { labelKey: 'apps.terminal.keys.tab', seq: '\t' },
  { labelKey: 'apps.terminal.keys.ctrl', ctrl: true },
  { labelKey: 'apps.terminal.keys.up', seq: '\x1b[A' },
  { labelKey: 'apps.terminal.keys.down', seq: '\x1b[B' },
  { labelKey: 'apps.terminal.keys.left', seq: '\x1b[D' },
  { labelKey: 'apps.terminal.keys.right', seq: '\x1b[C' },
  { labelKey: 'apps.terminal.keys.interrupt', seq: '\x03' },
  { labelKey: 'apps.terminal.keys.pipe', seq: '|' },
];

function buildWsUrl(sessionId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}/api/terminal/${encodeURIComponent(sessionId)}`;
}

export const TerminalView: React.FC<{ sessionId: string; onPersistent?: (persistent: boolean) => void }> = ({ sessionId, onPersistent }) => {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const ctrlStickyRef = useRef(false);
  const busyRetriesRef = useRef(0);
  const retryTimerRef = useRef<number | null>(null);
  // Report actual session persistence (from the backend 'ready' frame) up to the tab bar, so its
  // badge reflects reality — tmux-backed = persistent, plain-shell fallback = not. Held in a ref so
  // the WS effect (which doesn't depend on the prop) always calls the latest callback.
  const onPersistentRef = useRef(onPersistent);
  onPersistentRef.current = onPersistent;
  const [status, setStatus] = useState<Status>('connecting');
  const [exitCode, setExitCode] = useState<number | null>(null);
  const [reconnectKey, setReconnectKey] = useState(0);

  useEffect(() => {
    const term = new Terminal({
      fontSize: 13,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      cursorBlink: true,
      theme: { background: '#0b0b12' },
      allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    termRef.current = term;
    const refit = () => {
      const el = containerRef.current;
      // Skip when the container is hidden (a background tab uses display:none → 0×0): fitting to
      // zero would send a tiny {cols,rows} resize to the PTY and disrupt full-screen programs /
      // shells running in inactive tabs. The ResizeObserver fires again with real dimensions when
      // the tab is shown.
      if (!el || el.clientWidth === 0 || el.clientHeight === 0) return;
      try {
        fit.fit();
      } catch {
        /* container not measured yet */
      }
    };
    if (containerRef.current) {
      term.open(containerRef.current);
      refit();
      // The window opens with a scale transition (transform doesn't change layout size,
      // so the container's height is already real) but the first fit can still land before
      // the panel is laid out — refit on the next frame so xterm uses the FULL height
      // instead of getting stuck a few rows tall (the "only top half / can't scroll" bug).
      requestAnimationFrame(refit);
    }

    const onData = term.onData((data: string) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      let out = data;
      if (ctrlStickyRef.current && data.length === 1) {
        out = String.fromCharCode(data.toUpperCase().charCodeAt(0) & 0x1f);
        ctrlStickyRef.current = false;
      }
      ws.send(ENC.encode(out));
    });
    const onResize = term.onResize(({ cols, rows }: { cols: number; rows: number }) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'resize', cols, rows }));
    });

    setStatus('connecting');
    setExitCode(null);
    const ws = new WebSocket(buildWsUrl(sessionId));
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;
    ws.onopen = () => {
      try {
        refit();
      } catch {
        /* noop */
      }
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data === 'string') {
        try {
          const msg = JSON.parse(ev.data) as { type?: string; persistent?: boolean; code?: number };
          if (msg.type === 'ready') {
            busyRetriesRef.current = 0; // a successful attach resets the transient-retry budget
            setStatus('ready');
            onPersistentRef.current?.(!!msg.persistent);
          } else if (msg.type === 'exit') {
            setExitCode(typeof msg.code === 'number' ? msg.code : null);
            setStatus('closed');
          }
        } catch {
          /* ignore malformed control frame */
        }
        return;
      }
      term.write(new Uint8Array(ev.data as ArrayBuffer));
    };
    ws.onclose = (ev: CloseEvent) => {
      // 1013 = transient "try again shortly" (the session id is mid-open/teardown, or the cap
      // is momentarily full). Auto-retry a few times with a short backoff before surfacing an
      // error, so a reconnect that races a CLOSING teardown recovers on its own.
      if (ev.code === 1013 && busyRetriesRef.current < MAX_BUSY_RETRIES) {
        busyRetriesRef.current += 1;
        setStatus('connecting');
        retryTimerRef.current = window.setTimeout(
          () => setReconnectKey((k) => k + 1),
          250 * busyRetriesRef.current,
        );
        return;
      }
      setStatus((prev) =>
        prev === 'closed' ? prev : ev.code === 1008 ? 'disabled' : prev === 'ready' ? 'closed' : 'error',
      );
    };

    const ro = new ResizeObserver(() => refit());
    if (containerRef.current) ro.observe(containerRef.current);

    return () => {
      if (retryTimerRef.current != null) window.clearTimeout(retryTimerRef.current);
      ro.disconnect();
      onData.dispose();
      onResize.dispose();
      // Detach handlers before closing. A closing socket's onclose can fire asynchronously
      // *after* its replacement has already reported 'ready' (reconnect / effect remount);
      // left attached, the stale onclose would mark the live terminal 'closed' or schedule a
      // spurious 1013 reconnect. The torn-down terminal is being disposed, so its remaining
      // frames are moot — dropping them at this single chokepoint is the root fix.
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      try {
        ws.close();
      } catch {
        /* noop */
      }
      term.dispose();
      wsRef.current = null;
      termRef.current = null;
    };
  }, [sessionId, reconnectKey]);

  const sendKey = (k: { seq?: string; ctrl?: boolean }) => {
    if (k.ctrl) {
      ctrlStickyRef.current = !ctrlStickyRef.current;
      return;
    }
    const ws = wsRef.current;
    if (k.seq && ws && ws.readyState === WebSocket.OPEN) ws.send(ENC.encode(k.seq));
    termRef.current?.focus();
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-[11.5px] text-muted">
        <span
          className={clsx(
            'size-2 shrink-0 rounded-full',
            status === 'ready' ? 'bg-mint' : status === 'connecting' ? 'bg-amber-400' : 'bg-muted',
          )}
        />
        <span>
          {t(`apps.terminal.status.${status}`)}
          {status === 'closed' && exitCode != null ? ` · ${t('apps.terminal.exitCode', { code: exitCode })}` : ''}
        </span>
        {(status === 'closed' || status === 'error') && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="ml-auto h-6 gap-1 px-2 text-[11px]"
            onClick={() => {
              busyRetriesRef.current = 0;
              setReconnectKey((k) => k + 1);
            }}
          >
            <RotateCw className="size-3" /> {t('apps.terminal.reconnect')}
          </Button>
        )}
      </div>

      {status === 'disabled' ? (
        <div className="grid flex-1 place-items-center p-6 text-center text-[12.5px] text-muted">
          <div className="max-w-md">{t('apps.terminal.disabled')}</div>
        </div>
      ) : (
        <div ref={containerRef} className="min-h-0 flex-1 overflow-hidden bg-[#0b0b12] p-1.5" />
      )}

      {status !== 'disabled' && (
        // The accessory key bar shows on desktop too (design iwYIX): quick esc/tab/ctrl/arrows
        // without leaving the window. On phones it's essential (soft keyboards lack these keys).
        <div className="flex gap-1 overflow-x-auto border-t border-border bg-surface px-2 py-1.5">
          {KEYS.map((k) => (
            <button
              key={k.labelKey}
              type="button"
              onClick={() => sendKey(k)}
              className="shrink-0 rounded-md border border-border-strong px-2.5 py-1.5 font-mono text-[12px] text-foreground active:bg-foreground/[0.08]"
            >
              {t(k.labelKey)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
