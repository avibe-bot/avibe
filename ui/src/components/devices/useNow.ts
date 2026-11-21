import { useEffect, useState } from 'react';

/**
 * The time, advancing: what a page compares a sample's age with. A read that keeps failing leaves
 * the last sample on the page, and only a clock that moves lets that sample grow too old to speak
 * for now.
 */
export function useNow(everyMs = 15_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(timer);
  }, [everyMs]);
  return now;
}
