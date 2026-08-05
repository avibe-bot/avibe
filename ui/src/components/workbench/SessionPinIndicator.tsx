import clsx from 'clsx';
import { Pin } from 'lucide-react';

interface SessionPinIndicatorProps {
  pinned: boolean;
  label: string;
  className?: string;
}

// Passive pinned-state marker for mobile project rows, where the row action menu
// remains the mutation surface.
export const SessionPinIndicator: React.FC<SessionPinIndicatorProps> = ({ pinned, label, className }) => {
  if (!pinned) return null;

  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={clsx('flex shrink-0 text-cyan', className)}
    >
      <Pin className="size-3" aria-hidden="true" />
    </span>
  );
};
