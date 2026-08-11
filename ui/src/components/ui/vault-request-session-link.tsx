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
  const params = new URLSearchParams();
  if (requestId?.trim()) {
    params.set('vault_request', requestId.trim());
    // A request link is an explicit request to inspect the transcript. Do not
    // restore a remembered Show Page surface over that intent.
    params.set('view', 'chat');
  }
  const query = params.toString() ? `?${params.toString()}` : '';
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
