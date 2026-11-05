import { useTranslation } from 'react-i18next';
import { BellOff } from 'lucide-react';
import clsx from 'clsx';

import type { InboxCategory } from '../../lib/notificationPreferences';
import { Presence } from '../ui/presence';

const unreadLabel = (count: number) => (count > 99 ? '99+' : String(count));

/**
 * The head of an Inbox section (docs/plans/workbench-refinements.md §1): Bots or
 * Sessions, its unread count, and a "Muted" mark when the category's
 * notifications are off. A muted count is grey: still there to find, asking for
 * nothing. Shared by the Inbox popover and the Inbox page.
 */
export function InboxSectionHeader({
  category,
  unread,
  quiet,
  action,
  className,
  flipId,
}: {
  category: InboxCategory;
  unread: number;
  quiet: boolean;
  action?: React.ReactNode;
  className?: string;
  /** Its place in a moving list (`FlipList`). */
  flipId?: string;
}) {
  const { t } = useTranslation();
  return (
    <div data-flip-id={flipId} className={clsx('flex items-center gap-1.5', className)}>
      <span className="text-[11px] font-bold text-muted">
        {t(category === 'bot' ? 'workbench.inbox.sections.bots' : 'workbench.inbox.sections.sessions')}
      </span>
      <Presence show={unread > 0} value={unread}>
        {(count) => (
          <span
            className={clsx(
              'inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 font-mono text-[9px] font-bold',
              quiet ? 'bg-foreground/[0.18] text-foreground' : 'bg-cyan text-accent-foreground',
            )}
          >
            {unreadLabel(count)}
          </span>
        )}
      </Presence>
      {quiet && (
        <span className="inline-flex items-center gap-0.5 text-[10px] font-medium text-muted">
          <BellOff className="size-3" />
          {t('workbench.inbox.sections.muted')}
        </span>
      )}
      {action && <span className="ml-auto">{action}</span>}
    </div>
  );
}
