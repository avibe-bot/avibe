import { useEffect, useState } from 'react';

const minuteNow = () => Math.floor(Date.now() / 60_000);

/**
 * The minute it is now, moving on as minutes pass: what a list of memoized rows passes them so their
 * "3 minutes ago" moves on with the clock, rather than only when something else redraws the list.
 */
export function useMinute(): number {
  const [minute, setMinute] = useState(minuteNow);
  useEffect(() => {
    const timer = window.setInterval(() => setMinute(minuteNow()), 15_000);
    return () => window.clearInterval(timer);
  }, []);
  return minute;
}
