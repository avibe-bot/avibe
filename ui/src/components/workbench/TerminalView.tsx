import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { SearchAddon } from '@xterm/addon-search';
import { WebLinksAddon } from '@xterm/addon-web-links';
import '@xterm/xterm/css/xterm.css';
import { ChevronDown, ChevronUp, ClipboardPaste, RotateCw, Search, X } from 'lucide-react';

import { Button } from '../ui/button';
import { Input } from '../ui/input';
import {
  adjustTerminalFontSize,
  getTerminalFontSize,
  resetTerminalFontSize,
  subscribeTerminalFontSize,
} from '../../lib/terminalFontSize';

// xterm.js wired to the /api/terminal/{id} WebSocket. Protocol (locked with the
// backend): client sends raw stdin as BINARY frames and JSON control as TEXT
// frames ({type:"resize",cols,rows}); server sends PTY output as BINARY and
// {type:"ready"|"exit"} as TEXT. Lazy-loaded by AppsTerminalPage so xterm stays
// out of the main bundle.
export type TerminalStatus = 'connecting' | 'ready' | 'closed' | 'disabled' | 'error';

const ENC = new TextEncoder();
const MAX_BUSY_RETRIES = 3; // auto-retry a transient "busy" (1013) close this many times

// The terminal window is theme-locked to dark (registry lockTheme: a shell is conventionally
// dark, like a code editor), so xterm carries a fixed dark palette regardless of the global theme.
const TERMINAL_BG = '#0b0b12';
const TERMINAL_THEME = {
  background: TERMINAL_BG,
  foreground: '#e4e4e7',
  cursor: '#e4e4e7',
  cursorAccent: TERMINAL_BG,
  selectionBackground: 'rgba(148,163,184,0.35)',
};

// Search-match highlight colors for the SearchAddon. Amber reads clearly on the dark terminal and
// stays out of the app theme's way (the terminal is theme-locked dark). Colors must be #RRGGBB; the
// active (current) match is brighter than the rest. Decorations rely on the terminal's proposed
// decoration API — allowProposedApi is already enabled below. The overview-ruler colors are only
// painted when overviewRulerWidth is set (we don't set it), but the type requires them.
const SEARCH_DECORATIONS = {
  matchBackground: '#664d00',
  matchBorder: '#8a6d1a',
  matchOverviewRuler: '#b8860b',
  activeMatchBackground: '#b8860b',
  activeMatchBorder: '#ffcf5c',
  activeMatchColorOverviewRuler: '#ffcf5c',
};

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

function buildWsUrl(sessionId: string, cwd?: string | null): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const base = `${proto}://${window.location.host}/api/terminal/${encodeURIComponent(sessionId)}`;
  // `cwd` starts a NEW session in that directory ("Open Terminal Here"). The backend validates it
  // and ignores it when reattaching an existing session, so it's harmless to resend on reconnect.
  return cwd ? `${base}?cwd=${encodeURIComponent(cwd)}` : base;
}

// Apple vs. non-Apple decides the Find chord below. Detected once (navigator.platform is deprecated but
// remains the most reliable signal, with userAgent as a fallback).
const IS_APPLE =
  typeof navigator !== 'undefined' &&
  /Mac|iP(hone|ad|od)/i.test(navigator.platform || navigator.userAgent || '');

// The chord that opens Find: ⌘F on Apple platforms, Ctrl+Shift+F everywhere else. Plain Ctrl+F is a live
// terminal key on BOTH macOS and Linux (readline forward-char, less/vim page-forward), so it must keep
// reaching the PTY — hence ⌘ on Apple (a free modifier) and the Shift-qualified chord elsewhere, which is
// exactly what native terminals bind for find (GNOME Terminal, Konsole, Windows Terminal). Shared by the
// terminal key handler and the search field so both agree; altKey is excluded to avoid stray combos.
const isFindHotkey = (e: {
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  key: string;
}): boolean =>
  !e.altKey &&
  (e.key === 'f' || e.key === 'F') &&
  (IS_APPLE ? e.metaKey && !e.ctrlKey : e.ctrlKey && e.shiftKey && !e.metaKey);

// The font-zoom chord: ⌘ +/-/0 on Apple, Ctrl +/-/0 elsewhere — exactly what native terminal emulators
// (Terminal.app, iTerm, GNOME Terminal) and browsers bind for zoom. We claim it only while the terminal
// has focus (see the custom key handler), so it resizes the terminal font instead of the page; anywhere
// else in the app the browser's own zoom stays untouched. '+' and '=' are the same physical key (Shift
// decides which), so both mean zoom-in. Mirrors isFindHotkey: the modifier is ⌘ on Apple / Ctrl elsewhere
// and never both. All of -, =, +, 0 are non-control keys, so swallowing them steals nothing from the PTY;
// notably we do NOT match '_' (Ctrl+_ is 0x1f, readline's undo), leaving that control key for the shell.
type FontZoom = 'in' | 'out' | 'reset';
const fontZoomIntent = (e: {
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  key: string;
}): FontZoom | null => {
  if (e.altKey) return null;
  const modifier = IS_APPLE ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
  if (!modifier) return null;
  switch (e.key) {
    case '+':
    case '=':
      return 'in';
    case '-':
      return 'out';
    case '0':
      return 'reset';
    default:
      return null;
  }
};

export const TerminalView: React.FC<{
  sessionId: string;
  /** Start directory for a newly created session (from "Open Terminal Here"). Stable per tab. */
  cwd?: string | null;
  onPersistent?: (persistent: boolean) => void;
  /** Report connection status up so the tab bar can show one combined status + persistence chip. */
  onStatus?: (status: TerminalStatus, exitCode: number | null) => void;
}> = ({ sessionId, cwd, onPersistent, onStatus }) => {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const searchRef = useRef<SearchAddon | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const ctrlStickyRef = useRef(false);
  const busyRetriesRef = useRef(0);
  const retryTimerRef = useRef<number | null>(null);
  // Auto-dismiss timer for the mobile Paste key's "clipboard blocked" hint.
  const pasteHintTimerRef = useRef<number | null>(null);
  // Report actual session persistence (from the backend 'ready' frame) up to the tab bar, so its
  // badge reflects reality — tmux-backed = persistent, plain-shell fallback = not. Held in a ref so
  // the WS effect (which doesn't depend on the prop) always calls the latest callback.
  const onPersistentRef = useRef(onPersistent);
  onPersistentRef.current = onPersistent;
  const [status, setStatus] = useState<TerminalStatus>('connecting');
  const [exitCode, setExitCode] = useState<number | null>(null);
  const [reconnectKey, setReconnectKey] = useState(0);
  // Scrollback search (SearchAddon). Opened with ⌘F/Ctrl+F while the terminal has focus; results
  // carry {index,count} for the "3/12" match counter (index is -1 past the highlight threshold).
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<{ index: number; count: number }>({ index: -1, count: 0 });
  // Brief inline hint when the mobile Paste key can't read the clipboard (permission denied or an
  // insecure context) — a graceful notice instead of a silent failure or a crash.
  const [pasteBlocked, setPasteBlocked] = useState(false);

  // Surface connection status to the parent (tab bar). The standalone status row inside the
  // body was removed — only the terminating states render an in-body overlay (below).
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;
  useEffect(() => {
    onStatusRef.current?.(status, exitCode);
  }, [status, exitCode]);

  useEffect(() => {
    const term = new Terminal({
      fontSize: getTerminalFontSize(), // shared, persisted preference — see terminalFontSize.ts
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      cursorBlink: true,
      theme: TERMINAL_THEME,
      allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    // Clickable URLs open in a new tab, but only on a modifier-click (⌘ on Apple, Ctrl elsewhere).
    // Persistent sessions run tmux with `mouse on`, so an unmodified click is meaningful input for
    // tmux/copy-mode and mouse-aware TUIs — gating on the modifier keeps plain clicks as terminal input
    // and matches how VS Code / iTerm / GNOME Terminal follow terminal links. noopener defeats reverse-
    // tabnabbing (the addon's own typings call this out); window.open never navigates the app frame.
    term.loadAddon(
      new WebLinksAddon((event, uri) => {
        if (IS_APPLE ? event.metaKey : event.ctrlKey) {
          window.open(uri, '_blank', 'noopener');
        }
      }),
    );
    const search = new SearchAddon();
    term.loadAddon(search);
    searchRef.current = search;
    const searchResults = search.onDidChangeResults((e) =>
      setMatches({ index: e.resultIndex, count: e.resultCount }),
    );
    // Intercept the Find chord only while xterm's textarea has focus (this handler runs solely for
    // terminal key events), so the browser's own find stays available everywhere else in the app.
    // Returning false stops xterm forwarding the key to the PTY; preventDefault stops the browser find.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true;
      if (isFindHotkey(e)) {
        e.preventDefault();
        setSearchOpen(true);
        // Focus once the bar has mounted (first open) or refocus it (already open); rAF waits for commit.
        requestAnimationFrame(() => {
          searchInputRef.current?.focus();
          searchInputRef.current?.select();
        });
        return false;
      }
      const zoom = fontZoomIntent(e);
      if (zoom) {
        // Adjust the shared font size and swallow the key: preventDefault stops the browser zooming the
        // whole page, `return false` stops xterm forwarding ⌘=/⌘- to the PTY. Every mounted terminal
        // re-fits through the subscription below (so all tabs track one size) — this one included.
        e.preventDefault();
        if (zoom === 'reset') resetTerminalFontSize();
        else adjustTerminalFontSize(zoom === 'in' ? 1 : -1);
        return false;
      }
      return true;
    });
    termRef.current = term;
    const settleTimers: number[] = [];
    let resizeSendTimer: number | null = null;
    // Push the CURRENT terminal size to the PTY. The backend only learns the size from these
    // messages, and xterm's onResize fires ONLY on a change — so a no-op fit (the size already
    // settled before connect/reconnect) would otherwise never tell the PTY, leaving it at the
    // default 24x80. A maximized window then shows the shell using only the top ~24 rows with a
    // big blank area below (and the cursor stuck partway up).
    const sendSize = () => {
      const term = termRef.current;
      const ws = wsRef.current;
      if (term && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      }
    };
    // Debounced so a maximize/restore animation's flurry of fits sends just the final size.
    const queueSendSize = () => {
      if (resizeSendTimer != null) window.clearTimeout(resizeSendTimer);
      resizeSendTimer = window.setTimeout(sendSize, 80);
    };
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
      // Always (re)send the fitted size — covers the no-op fit at connect/reconnect that onResize misses.
      queueSendSize();
    };
    // A single fit can land before BOTH the layout and xterm's own character-cell metrics have
    // settled. On some browsers (notably Safari) the cell size finalises a frame or two after
    // open while the CONTAINER box never changes again — so the ResizeObserver never fires to
    // correct an initial under-fit, and the terminal stays only a few rows tall with the rest of
    // the window blank ("only the top third renders"). Re-fit across the next couple of frames
    // plus a short tail of timeouts so the row count converges no matter when metrics settle;
    // once it's correct, fit() is a no-op.
    const settle = () => {
      refit();
      requestAnimationFrame(() => {
        refit();
        requestAnimationFrame(refit);
      });
      settleTimers.push(window.setTimeout(refit, 120), window.setTimeout(refit, 360));
    };
    if (containerRef.current) {
      term.open(containerRef.current);
      settle();
    }

    // Apply later font-size changes — this terminal's own zoom hotkey, or another terminal's — to this
    // instance: set the size, then reuse the existing refit path so the new cols/rows reach the PTY. The
    // starting size is read once in the constructor above; subscribe fires only on subsequent changes.
    const unsubscribeFontSize = subscribeTerminalFontSize((size) => {
      term.options.fontSize = size;
      refit();
    });

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
    setStatus('connecting');
    setExitCode(null);
    setMatches({ index: -1, count: 0 }); // a reconnect rebuilds the SearchAddon; drop the stale count
    const ws = new WebSocket(buildWsUrl(sessionId, cwd));
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
            // The shell is live and about to paint its first content — settle the fit again so a
            // late cell-metric correction doesn't leave the terminal a few rows tall.
            settle();
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
      if (resizeSendTimer != null) window.clearTimeout(resizeSendTimer);
      for (const id of settleTimers) window.clearTimeout(id);
      ro.disconnect();
      onData.dispose();
      searchResults.dispose();
      unsubscribeFontSize();
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
      searchRef.current = null;
    };
  }, [sessionId, cwd, reconnectKey]);

  // Clear the paste-hint auto-dismiss timer if the view unmounts before it fires.
  useEffect(
    () => () => {
      if (pasteHintTimerRef.current != null) window.clearTimeout(pasteHintTimerRef.current);
    },
    [],
  );

  const sendKey = (k: { seq?: string; ctrl?: boolean }) => {
    if (k.ctrl) {
      ctrlStickyRef.current = !ctrlStickyRef.current;
      return;
    }
    const ws = wsRef.current;
    if (k.seq && ws && ws.readyState === WebSocket.OPEN) ws.send(ENC.encode(k.seq));
    termRef.current?.focus();
  };

  // Mobile Paste key: read the clipboard and feed it to the PTY through xterm's paste(), which applies
  // bracketed-paste framing when the running program asked for it (so a multi-line paste isn't executed
  // line by line in shells/editors that opted in). Exists because the touch key bar has no paste; desktop
  // users paste with the hardware keyboard. The read is intentionally un-guarded (no `?.`) so a blocked
  // clipboard — permission denied, or a non-secure context where the API is absent — throws into the
  // catch and surfaces a brief hint, rather than failing silently.
  const pasteFromClipboard = async () => {
    const term = termRef.current;
    try {
      const text = await navigator.clipboard.readText();
      const ws = wsRef.current;
      if (text && term && ws && ws.readyState === WebSocket.OPEN) term.paste(text);
      term?.focus();
    } catch {
      setPasteBlocked(true);
      if (pasteHintTimerRef.current != null) window.clearTimeout(pasteHintTimerRef.current);
      pasteHintTimerRef.current = window.setTimeout(() => setPasteBlocked(false), 3200);
    }
  };

  const reconnect = () => {
    busyRetriesRef.current = 0;
    setReconnectKey((k) => k + 1);
  };

  // Step between matches. Incremental (type-ahead) applies only to findNext — it grows the current
  // selection while it still matches; explicit next/prev jump to the adjacent match.
  const runFind = (direction: 'next' | 'prev', incremental = false) => {
    const s = searchRef.current;
    if (!s || !query) return;
    if (direction === 'prev') s.findPrevious(query, { decorations: SEARCH_DECORATIONS });
    else s.findNext(query, { decorations: SEARCH_DECORATIONS, incremental });
  };

  const onQueryChange = (value: string) => {
    setQuery(value);
    const s = searchRef.current;
    if (!s) return;
    if (!value) {
      s.clearDecorations();
      setMatches({ index: -1, count: 0 });
      return;
    }
    s.findNext(value, { decorations: SEARCH_DECORATIONS, incremental: true });
  };

  const closeSearch = () => {
    setSearchOpen(false);
    setQuery(''); // drop the term so reopening starts fresh instead of showing a stale "0/0" counter
    searchRef.current?.clearDecorations();
    setMatches({ index: -1, count: 0 });
    termRef.current?.focus();
  };

  const onSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    // Mid-IME-composition (e.g. a CJK candidate), Enter/Escape belong to the IME — accepting or
    // cancelling the candidate — not to search navigation. Let them through untouched.
    if (e.nativeEvent.isComposing) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      runFind(e.shiftKey ? 'prev' : 'next');
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closeSearch();
    } else if (isFindHotkey(e)) {
      // Focus is already in the field — keep the browser find bar from opening; reselect instead.
      e.preventDefault();
      e.currentTarget.select();
    }
  };

  const matchLabel =
    matches.count === 0 ? '0/0' : `${matches.index >= 0 ? matches.index + 1 : '?'}/${matches.count}`;

  return (
    <div className="flex h-full min-h-0 flex-col" style={{ backgroundColor: TERMINAL_BG }}>
      {status === 'disabled' ? (
        <div className="grid flex-1 place-items-center p-6 text-center text-[12.5px] text-muted">
          <div className="max-w-md">{t('apps.terminal.disabled')}</div>
        </div>
      ) : (
        <div className="relative min-h-0 flex-1">
          <div ref={containerRef} className="absolute inset-0 overflow-hidden p-1.5" />
          {searchOpen && (
            // Compact find widget pinned to the terminal's top-right, like an editor's find bar. Colors
            // are fixed-dark (not theme tokens) because the terminal body is theme-locked dark — the
            // same reason the touch key bar below uses fixed colors rather than the global palette.
            <div className="absolute right-2 top-2 z-10 flex items-center gap-0.5 rounded-lg border border-zinc-700/80 bg-zinc-900/95 px-1.5 py-1 shadow-lg backdrop-blur-sm">
              <Search className="ml-0.5 size-3.5 shrink-0 text-zinc-500" aria-hidden />
              <Input
                ref={searchInputRef}
                variant="bare"
                value={query}
                onChange={(e) => onQueryChange(e.target.value)}
                onKeyDown={onSearchKeyDown}
                placeholder={t('apps.terminal.search.placeholder')}
                aria-label={t('apps.terminal.search.ariaLabel')}
                spellCheck={false}
                autoComplete="off"
                className="h-6 w-36 px-1 text-[12px] text-zinc-100 placeholder:text-zinc-500 sm:w-48"
              />
              {query && (
                <span
                  className="shrink-0 px-1 text-[11px] tabular-nums text-zinc-500"
                  aria-live="polite"
                >
                  {matchLabel}
                </span>
              )}
              <button
                type="button"
                onClick={() => runFind('prev')}
                disabled={matches.count === 0}
                aria-label={t('apps.terminal.search.prev')}
                className="grid size-6 shrink-0 place-items-center rounded text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40 disabled:hover:bg-transparent"
              >
                <ChevronUp className="size-3.5" />
              </button>
              <button
                type="button"
                onClick={() => runFind('next')}
                disabled={matches.count === 0}
                aria-label={t('apps.terminal.search.next')}
                className="grid size-6 shrink-0 place-items-center rounded text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40 disabled:hover:bg-transparent"
              >
                <ChevronDown className="size-3.5" />
              </button>
              <button
                type="button"
                onClick={closeSearch}
                aria-label={t('apps.terminal.search.close')}
                className="grid size-6 shrink-0 place-items-center rounded text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
              >
                <X className="size-3.5" />
              </button>
            </div>
          )}
          {(status === 'closed' || status === 'error') && (
            // The "connected" status no longer occupies its own row — it lives in the tab bar.
            // Only the terminating states surface in the body, as a centred overlay that offers
            // a reconnect (the dimmed last output stays visible underneath).
            <div className="absolute inset-0 grid place-items-center bg-surface/85 p-6 text-center backdrop-blur-[1px]">
              <div className="flex flex-col items-center gap-3">
                <span className="text-[12.5px] text-muted">
                  {t(`apps.terminal.status.${status}`)}
                  {status === 'closed' && exitCode != null ? ` · ${t('apps.terminal.exitCode', { code: exitCode })}` : ''}
                </span>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-7 gap-1.5 px-3 text-[12px]"
                  onClick={reconnect}
                >
                  <RotateCw className="size-3" /> {t('apps.terminal.reconnect')}
                </Button>
              </div>
            </div>
          )}
          {pasteBlocked && (
            // The mobile Paste key couldn't read the clipboard — float a brief, non-blocking notice above
            // the key bar. Fixed-dark like the find widget (the terminal body is theme-locked dark);
            // pointer-events-none so it never intercepts a tap. Only the touch-only Paste key sets this.
            <div className="pointer-events-none absolute inset-x-0 bottom-2 z-10 flex justify-center px-3">
              <span className="rounded-md bg-zinc-900/95 px-3 py-1.5 text-center text-[11px] text-zinc-200 shadow-lg ring-1 ring-zinc-700/70">
                {t('apps.terminal.pasteBlocked')}
              </span>
            </div>
          )}
        </div>
      )}

      {status !== 'disabled' && (
        // Accessory key bar for touch devices only — soft keyboards lack esc/tab/ctrl/arrows.
        // Hidden when the primary pointer is fine (desktop, incl. touchscreen laptops): a
        // hardware keyboard already has these keys and the bar just costs a row. Keyed off
        // pointer type rather than the md: viewport breakpoint so tablets keep it.
        <div className="hidden gap-1 overflow-x-auto border-t border-border bg-surface px-2 py-1.5 pointer-coarse:flex">
          <button
            type="button"
            onClick={pasteFromClipboard}
            aria-label={t('apps.terminal.keys.paste')}
            className="flex shrink-0 items-center gap-1 rounded-md border border-border-strong px-2.5 py-1.5 text-[12px] text-foreground active:bg-foreground/[0.08]"
          >
            <ClipboardPaste className="size-3.5" aria-hidden />
            {t('apps.terminal.keys.paste')}
          </button>
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
