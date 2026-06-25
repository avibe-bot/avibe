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

// Accessory key bar for phones (their soft keyboards lack these). Each button
// sends the raw byte sequence the PTY expects; Ctrl is a sticky modifier.
const KEYS: { label: string; seq?: string; ctrl?: boolean }[] = [
  { label: 'Esc', seq: '\x1b' },
  { label: 'Tab', seq: '\t' },
  { label: 'Ctrl', ctrl: true },
  { label: '↑', seq: '\x1b[A' },
  { label: '↓', seq: '\x1b[B' },
  { label: '←', seq: '\x1b[D' },
  { label: '→', seq: '\x1b[C' },
  { label: '^C', seq: '\x03' },
  { label: '|', seq: '|' },
];

function buildWsUrl(sessionId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}/api/terminal/${encodeURIComponent(sessionId)}`;
}

export const TerminalView: React.FC<{ sessionId: string }> = ({ sessionId }) => {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const ctrlStickyRef = useRef(false);
  const [status, setStatus] = useState<Status>('connecting');
  const [persistent, setPersistent] = useState(false);
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
    if (containerRef.current) {
      term.open(containerRef.current);
      try {
        fit.fit();
      } catch {
        /* container not measured yet */
      }
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
        fit.fit();
      } catch {
        /* noop */
      }
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data === 'string') {
        try {
          const msg = JSON.parse(ev.data) as { type?: string; persistent?: boolean; code?: number };
          if (msg.type === 'ready') {
            setStatus('ready');
            setPersistent(!!msg.persistent);
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
      setStatus((prev) =>
        prev === 'closed' ? prev : ev.code === 1008 ? 'disabled' : prev === 'ready' ? 'closed' : 'error',
      );
    };

    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* noop */
      }
    });
    if (containerRef.current) ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      onData.dispose();
      onResize.dispose();
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
        {status === 'ready' && persistent && (
          <span className="rounded-full border border-border px-1.5 text-[10px]">{t('apps.terminal.persistent')}</span>
        )}
        {(status === 'closed' || status === 'error') && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="ml-auto h-6 gap-1 px-2 text-[11px]"
            onClick={() => setReconnectKey((k) => k + 1)}
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
        <div className="flex gap-1 overflow-x-auto border-t border-border bg-surface px-2 py-1.5 md:hidden">
          {KEYS.map((k) => (
            <button
              key={k.label}
              type="button"
              onClick={() => sendKey(k)}
              className="shrink-0 rounded-md border border-border-strong px-2.5 py-1.5 font-mono text-[12px] text-foreground active:bg-foreground/[0.08]"
            >
              {k.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
