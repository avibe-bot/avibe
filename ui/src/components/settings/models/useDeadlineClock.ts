import * as React from 'react';

const CLOCK_TICK_MS = 60_000;

export const useDeadlineClock = (deadlineValue?: string | null): number => {
  const [now, setNow] = React.useState(() => Date.now());
  const deadline = deadlineValue ? Date.parse(deadlineValue) : Number.NaN;

  React.useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      const current = Date.now();
      setNow(current);
      if (Number.isFinite(deadline) && current < deadline) {
        timer = setTimeout(tick, Math.min(CLOCK_TICK_MS, deadline - current));
      }
    };

    tick();
    return () => {
      if (timer !== null) clearTimeout(timer);
    };
  }, [deadline]);

  return now;
};
