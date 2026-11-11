import { cn } from '@/lib/utils';

/** The mark of one option in a single choice: a ring, filled when chosen. */
export const RadioDot: React.FC<{ selected: boolean; className?: string }> = ({ selected, className }) => (
  <span
    aria-hidden="true"
    className={cn(
      'flex size-[18px] shrink-0 items-center justify-center rounded-full border-2 transition-colors',
      selected ? 'border-mint' : 'border-border-strong',
      className,
    )}
  >
    {selected && <span className="size-2 rounded-full bg-mint" />}
  </span>
);
