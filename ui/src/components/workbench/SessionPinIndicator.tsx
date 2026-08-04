import clsx from 'clsx';
import { Pin } from 'lucide-react';

interface SessionPinIndicatorProps {
  pinned: boolean;
  label: string;
  className?: string;
}

// Passive "this row is pinned" status. Not a button: pinning moved into the row's
// ⋯ action menu (see sessionActions.tsx), so this glyph only has to keep the state
// visible at a glance — including when the row is not hovered.
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
