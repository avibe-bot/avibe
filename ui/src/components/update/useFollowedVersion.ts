// The version of the Avibe this page belongs to, for a chip that has no panel of its own: the desktop app's
// bottom row on another computer's instance, where the update panel is the shell's, in another window.
import { useCallback, useEffect, useState } from 'react';
import { useApi, type VersionInfo } from '../../context/ApiContext';
import { useFollowInstall, useReadOnReturn, WATCH_LIMIT_MS, WATCH_POLL_MS } from './useFollowInstall';

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
  // The watch ends when this window is in front again, whether or not an install is being followed by then.
  useEffect(() => {
    if (!watching) return;
    const back = () => setWatching(false);
    window.addEventListener('focus', back);
    // The shell may not have presented the panel at all; the watch does not outlast it by long.
    const limit = window.setTimeout(back, WATCH_LIMIT_MS);
    return () => {
      window.removeEventListener('focus', back);
      window.clearTimeout(limit);
    };
  }, [watching]);
  // A followed install has its own, faster reads.
  useEffect(() => {
    if (!watching || updating) return;
    const timer = window.setInterval(reread, WATCH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [watching, updating, reread]);
  return { info, updating, watch: useCallback(() => setWatching(true), []) };
}
