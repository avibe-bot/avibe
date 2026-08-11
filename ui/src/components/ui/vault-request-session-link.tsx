import { Link } from 'react-router-dom';

import { cn } from '@/lib/utils';
import type { VaultRequestSessionDisplay } from './vault-request-session';

export const VaultRequestSessionLink: React.FC<{
  session: VaultRequestSessionDisplay;
  requestId?: string;
  className?: string;
  textClassName?: string;
}> = ({ session, requestId, className, textClassName }) => {
  const label = (
    <span className={cn('min-w-0 truncate', session.isIdFallback && 'font-mono', textClassName)}>
      {session.label}
    </span>
  );
  if (!session.isWorkbench) {
    return <span className={cn('min-w-0 truncate', className)}>{label}</span>;
  }
  const query = requestId?.trim()
    ? `?${new URLSearchParams({ vault_request: requestId.trim() }).toString()}`
    : '';
  return (
    <Link
      to={`/chat/${encodeURIComponent(session.id)}${query}`}
      className={cn(
        'min-w-0 truncate font-medium text-foreground transition-colors hover:text-cyan hover:underline',
        className,
      )}
    >
      {label}
    </Link>
  );
};
