import clsx from 'clsx';
import { Loader2, Pin } from 'lucide-react';
import type { MouseEventHandler } from 'react';

interface SessionPinActionProps {
  pinned: boolean;
  pending: boolean;
  pinLabel: string;
  unpinLabel: string;
  onToggle: () => void;
  className?: string;
}

export const SessionPinAction: React.FC<SessionPinActionProps> = ({
  pinned,
  pending,
  pinLabel,
  unpinLabel,
  onToggle,
  className,
}) => {
  const label = pinned ? unpinLabel : pinLabel;
  const handleClick: MouseEventHandler<HTMLButtonElement> = (event) => {
    event.stopPropagation();
    onToggle();
  };

  return (
    <button
      type="button"
      disabled={pending}
      aria-label={label}
      aria-pressed={pinned}
      title={label}
      onClick={handleClick}
      className={clsx(
        'group/pin grid size-6 shrink-0 place-items-center rounded-md transition-[opacity,transform,background-color,color,box-shadow] duration-150 ease-out',
        'hover:-translate-y-px hover:scale-105 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan/60 motion-reduce:transform-none',
        pending
          ? 'cursor-wait text-muted opacity-100 hover:translate-y-0 hover:scale-100'
          : pinned
            ? 'bg-cyan/[0.10] text-cyan opacity-100 hover:bg-cyan/[0.18] hover:ring-1 hover:ring-cyan/30'
            : 'text-muted opacity-0 hover:bg-foreground/[0.08] hover:text-foreground group-hover/sess:opacity-100 group-focus-within/sess:opacity-100 pointer-coarse:opacity-100',
        className,
      )}
    >
      {pending ? (
        <Loader2 className="size-3 animate-spin" aria-hidden="true" />
      ) : (
        <Pin
          className="size-3 transition-transform duration-150 group-hover/pin:-rotate-12 group-hover/pin:scale-110 motion-reduce:transform-none"
          aria-hidden="true"
        />
      )}
    </button>
  );
};
