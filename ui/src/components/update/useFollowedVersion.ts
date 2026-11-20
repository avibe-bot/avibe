// The version of the Avibe this page belongs to, kept current for a chip that has no panel of its own
// (the desktop app's bottom row on another computer's instance). The page reads its own `/api/version`,
// as a browser does; it reads again when the window comes back to the front (the update panel is another
// window there), and follows an install to its end, whoever started it.
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi, type VersionInfo } from '../../context/ApiContext';
import { installRunning, normalizeVersion } from './versions';

// A standalone Runtime looks its latest release up on every version read, so these stay unhurried.
const FOLLOW_POLL_MS = 3000;
const WATCH_POLL_MS = 5000;
/** A read on return to the window is skipped when the last one is this fresh. */
const REFRESH_MIN_INTERVAL_MS = 5000;

/** Follows an install on this Avibe from `info` to its end: rereads the version until nothing is running, and
 *  loads the page again once the Runtime answers with another version, since its interface changed with it.
 *  Every read of the version while an install is followed goes through `receive`, whichever path made it, so
 *  none can show the new version on the old page. */
export function useFollowInstall(info: VersionInfo | null, onRead: (info: VersionInfo) => void, enabled = true): {
  running: boolean;
  receive: (fresh: VersionInfo) => void;
} {
  const api = useApi();
  const running = enabled && installRunning(info);
  // What the callbacks below read, without being rebuilt on every read.
  const latest = useRef({ info, onRead });
  useEffect(() => { latest.current = { info, onRead }; });
  // The version the followed install started from, kept until a read shows it ended.
  const started = useRef<string | null>(null);
  useEffect(() => {
    if (running && started.current === null) started.current = latest.current.info?.current ?? null;
  }, [running]);
  const receive = useCallback((fresh: VersionInfo) => {
    if (started.current && normalizeVersion(fresh.current) !== normalizeVersion(started.current)) {
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

export function useFollowedVersion(): { info: VersionInfo | null; updating: boolean; watch: () => void } {
  const api = useApi();
  const [info, setInfo] = useState<VersionInfo | null>(null);
  const { running: updating, receive } = useFollowInstall(info, setInfo);
  // The shell's panel is another window and takes the focus: from the moment this page asks for it until this
  // window is in front again, the page keeps reading, so an install started in the panel shows here at once.
  const [watching, setWatching] = useState(false);
  const reread = useCallback(() => {
    void api.getVersion({ cache: false, background: true }).then(receive).catch(() => undefined);
  }, [api, receive]);
  useEffect(() => {
    let active = true;
    void api.getVersion().then((next) => { if (active) setInfo(next); }).catch(() => undefined);
    return () => { active = false; };
  }, [api]);
  useReadOnReturn(true, reread);
  useEffect(() => {
    if (!watching || updating) return;
    const timer = window.setInterval(reread, WATCH_POLL_MS);
    const back = () => setWatching(false);
    window.addEventListener('focus', back);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', back);
    };
  }, [watching, updating, reread]);
  return { info, updating, watch: useCallback(() => setWatching(true), []) };
}
