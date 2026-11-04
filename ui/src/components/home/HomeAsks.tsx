import clsx from 'clsx';

import { useIsDesktop } from '@/lib/useIsDesktop';
import type { HomeAsk } from './homeCopy';

/** Four equal cells (two by two on a phone). A tap fills the composer with the whole ask; it never
 *  sends. `quiet` while there is no model: one thing leads at a time, and it is not these. */
export function HomeAsks({ asks, quiet, onAsk }: { asks: HomeAsk[]; quiet: boolean; onAsk: (ask: string) => void }) {
  const desktop = useIsDesktop();
  return (
    <div className={clsx('grid w-full gap-2', desktop ? 'grid-cols-4' : 'grid-cols-2')}>
      {asks.map(({ key, icon: Icon, label, ask }) => (
        <button
          key={key}
          type="button"
          title={ask}
          disabled={quiet}
          onClick={() => onAsk(ask)}
          className={clsx(
            'flex min-w-0 items-center gap-2.5 rounded-xl border border-border bg-surface px-3 text-sm text-foreground transition hover:border-mint/40 hover:bg-mint-soft hover:text-mint-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50',
            desktop ? 'h-12' : 'h-11',
          )}
        >
          <span className={clsx('grid size-7 shrink-0 place-items-center rounded-lg', quiet ? 'bg-surface-2 text-muted' : 'bg-mint-soft text-mint-ink')}>
            <Icon className="size-4" />
          </span>
          <span className="truncate">{label}</span>
        </button>
      ))}
    </div>
  );
}
