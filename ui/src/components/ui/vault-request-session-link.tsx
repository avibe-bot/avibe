import { Link } from 'react-router-dom';

import type { VaultRequest } from '@/context/ApiContext';
import { cn } from '@/lib/utils';

export type VaultRequestSessionDisplay = {
  id: string;
  label: string;
  isWorkbench: boolean;
  isIdFallback: boolean;
};

export function vaultRequestSessionDisplay(request: VaultRequest): VaultRequestSessionDisplay | null {
  const card = (request.card ?? {}) as { session_id?: unknown };
  const cardSessionId = typeof card.session_id === 'string' && card.session_id.trim() ? card.session_id.trim() : null;
  const session = request.session ?? null;
  const id = session?.id?.trim() || cardSessionId;
  if (!id) return null;
  const title = session?.title?.trim();
  const sessionLabel = session?.label?.trim();
  const label = title || sessionLabel || id;
  return {
    id,
    label,
    isWorkbench: Boolean(session?.is_workbench),
    isIdFallback: label === id && !title && (!sessionLabel || sessionLabel === id),
  };
}

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
        'min-w-0 truncate font-medium text-foreground transition-colors hover:text-cyan hover:underline',
        className,
      )}
    >
      {label}
    </Link>
  );
};
