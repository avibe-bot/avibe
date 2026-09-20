import { Link } from 'react-router-dom';

import { cn } from '@/lib/utils';
import type { VaultRequestSessionDisplay } from './vault-request-session';

export const VaultRequestSessionLink: React.FC<{
  session: VaultRequestSessionDisplay;
  className?: string;
  textClassName?: string;
}> = ({ session, className, textClassName }) => {
  const label = (
    <span className={cn('min-w-0 truncate', session.isIdFallback && 'font-mono', textClassName)}>
      {session.label}
    </span>
  );
  if (!session.isWorkbench) {
    return <span className={cn('min-w-0 truncate', className)}>{label}</span>;
  }
  return (
    <Link
      to={`/chat/${encodeURIComponent(session.id)}`}
      className={cn(
        'min-w-0 truncate font-medium text-foreground transition-colors hover:text-cyan-ink hover:underline',
        className,
      )}
    >
      {label}
    </Link>
  );
};
