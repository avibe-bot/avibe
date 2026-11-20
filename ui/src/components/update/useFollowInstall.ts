// Keeping the version of the Avibe this page belongs to current: the page reads its own `/api/version`, as a
// browser does, again when its window comes back to the front, and follows an install to its end, whoever
// started it.
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi, type VersionInfo } from '../../context/ApiContext';
import { installRunning, normalizeVersion } from './versions';

// A standalone Runtime looks its latest release up on every version read, so these stay unhurried.
const FOLLOW_POLL_MS = 3000;
export const WATCH_POLL_MS = 5000;
// Both end by themselves: an install the Runtime never finishes reporting, or a panel that never opened.
const FOLLOW_LIMIT_MS = 30 * 60_000;
export const WATCH_LIMIT_MS = 2 * 60_000;
/** A read on return to the window is skipped when the last one is this fresh. */
const REFRESH_MIN_INTERVAL_MS = 5000;

/** Follows an install on this Avibe from `info` to its end: rereads the version until nothing is running, and
 *  loads the page again once the Runtime answers with a version other than the one it had, since its
 *  interface changed with it.
 *  Every read of the version while an install is followed goes through `receive`, whichever path made it, so
 *  none can show the new version on the old page. */
export function useFollowInstall(info: VersionInfo | null, onRead: (info: VersionInfo) => void, enabled = true): {
  running: boolean;
  receive: (fresh: VersionInfo) => void;
} {
  const api = useApi();
  // The install being reported, by what tells it from the next one.
  const reported = enabled && installRunning(info)
    ? info?.install?.started_at ?? info?.remote_request?.request_id ?? 'install' : null;
  // Following one install stops after its limit; another install is followed afresh.
  const [gaveUpOn, setGaveUpOn] = useState<string | null>(null);
  const running = reported !== null && reported !== gaveUpOn;
  useEffect(() => {
    if (reported === null) return;
    const timer = window.setTimeout(() => setGaveUpOn(reported), FOLLOW_LIMIT_MS);
    return () => window.clearTimeout(timer);
  }, [reported]);
  // What the callbacks below read, without being rebuilt on every read.
  const latest = useRef({ info, onRead });
  // The version this page was loaded with: the first one it read.
  const loaded = useRef<string | null>(null);
  useEffect(() => {
    latest.current = { info, onRead };
    if (loaded.current === null && info) loaded.current = info.current;
  });
  // The version the followed install started from, kept until a read shows it ended.
  const started = useRef<string | null>(null);
  useEffect(() => {
    if (running && started.current === null) started.current = latest.current.info?.current ?? null;
  }, [running]);
  const receive = useCallback((fresh: VersionInfo) => {
    // Also when no install was seen on the way: it may have started and ended between two reads.
    const from = started.current ?? loaded.current;
    if (from && normalizeVersion(fresh.current) !== normalizeVersion(from)) {
      window.location.reload();
      return;
    }
    // Ended on the version it started from: nothing changed on this page.
    if (!installRunning(fresh)) started.current = null;
    latest.current.onRead(fresh);
  }, []);
  useEffect(() => {
    if (!running) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const fresh = await api.getVersion({ cache: false, background: true });
        if (stopped) return;
        receive(fresh);
      } catch {
        // The Runtime does not answer while it restarts; the next read finds it.
      }
      if (!stopped) timer = window.setTimeout(() => void poll(), FOLLOW_POLL_MS);
    };
    timer = window.setTimeout(() => void poll(), FOLLOW_POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [api, running, receive]);
  return { running, receive };
}

/** Calls `read` when this page's window comes back to the front or becomes visible again, at most once every
 *  few seconds: what was read before may have changed while the page was not being looked at. */
export function useReadOnReturn(enabled: boolean, read: () => void): void {
  useEffect(() => {
    if (!enabled) return;
    let last = 0;
    const back = () => {
      if (document.visibilityState !== 'visible' || Date.now() - last < REFRESH_MIN_INTERVAL_MS) return;
      last = Date.now();
      read();
    };
    window.addEventListener('focus', back);
    document.addEventListener('visibilitychange', back);
    return () => {
      window.removeEventListener('focus', back);
      document.removeEventListener('visibilitychange', back);
    };
  }, [enabled, read]);
}
