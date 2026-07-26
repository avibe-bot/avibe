import { useEffect, useState } from 'react';

export const VISIBLE_NOW_INTERVAL_MS = 30_000;

export interface VisibleTickerHost {
  isVisible: () => boolean;
  setInterval: (callback: () => void, intervalMs: number) => number;
  clearInterval: (timer: number) => void;
  addVisibilityListener: (callback: () => void) => void;
  removeVisibilityListener: (callback: () => void) => void;
}

const browserHost: VisibleTickerHost = {
  isVisible: () => document.visibilityState === 'visible',
  setInterval: (callback, intervalMs) => window.setInterval(callback, intervalMs),
  clearInterval: (timer) => window.clearInterval(timer),
  addVisibilityListener: (callback) => document.addEventListener('visibilitychange', callback),
  removeVisibilityListener: (callback) => document.removeEventListener('visibilitychange', callback),
};

export function startVisibleTicker(
  onTick: () => void,
  intervalMs: number = VISIBLE_NOW_INTERVAL_MS,
  host: VisibleTickerHost = browserHost,
): () => void {
  let timer: number | null = null;

  const stop = () => {
    if (timer === null) return;
    host.clearInterval(timer);
    timer = null;
  };
  const sync = () => {
    if (!host.isVisible()) {
      stop();
      return;
    }
    onTick();
    if (timer === null) timer = host.setInterval(onTick, intervalMs);
  };

  sync();
  host.addVisibilityListener(sync);
  return () => {
    stop();
    host.removeVisibilityListener(sync);
  };
}

export function useVisibleNow(intervalMs: number = VISIBLE_NOW_INTERVAL_MS): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(
    () => startVisibleTicker(() => setNow(Date.now()), intervalMs),
    [intervalMs],
  );
  return now;
}
