import { ArrowUpRight } from 'lucide-react';
import { Link } from 'react-router-dom';

import { cn } from '@/lib/utils';
import type { VaultRequestSessionDisplay } from './vault-request-session';

/**
 * The Session that asked, as a request names it. A Workbench Session is a link that reads as one
 * without a hover (a phone has none): the link ink and an arrow, like the other in-app links. It
 * opens that conversation, where the request's own card waits until it is answered. A Session
 * the Workbench cannot open (an IM chat) is its plain name.
 */
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
    return <span className={cn('min-w-0 truncate text-foreground', className)}>{label}</span>;
  }
  return (
    <Link
      to={`/chat/${encodeURIComponent(session.id)}`}
      className={cn(
        'inline-flex min-w-0 items-center gap-0.5 font-medium text-cyan-ink underline-offset-2 hover:underline',
        className,
      )}
    >
      {label}
      <ArrowUpRight aria-hidden className="size-3 shrink-0" />
    </Link>
  );
};
