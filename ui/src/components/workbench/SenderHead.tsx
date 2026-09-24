import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { formatLocalClockTime, formatLocalDateTime } from '../../lib/relativeTime';
import { SENDER_TONE_CLASS, senderInitial, senderTone } from '../../lib/senderIdentity';

// The name/time/avatar line above a human bubble on an Organization instance
// (design.pen nlrCu). Several people share that transcript, so a bubble with no
// name cannot be attributed; on a personal instance every human row is the
// owner, and this head is never rendered there.
//
// Ordered to read outward from the bubble it heads — name, time, then the
// avatar against the transcript edge — matching the right-aligned row.
//
// Lives beside RoleAvatar rather than inside ChatPage so a row component can
// use it without importing the whole page back.
export const SenderHead: React.FC<{
  authorId: string | null;
  label: string | null | undefined;
  createdAt: string;
}> = ({ authorId, label, createdAt }) => {
  const { t } = useTranslation();
  // No label means the server could not resolve this principal — an IM-relayed
  // human row, or a subject Cloud never stored an email for. Naming it anything
  // but "unknown" would attribute the message to someone.
  const resolved = typeof label === 'string' && label.trim() ? label.trim() : null;

  return (
    <div className="flex max-w-full items-center gap-2.5 px-1">
      <span className="truncate text-[13px] font-bold text-foreground">
        {resolved ?? t('chat.senderUnknown')}
      </span>
      {/* Compact clock beside the name; the exact stamp stays one hover away. */}
      <span
        className="shrink-0 text-[12px] font-medium text-muted"
        title={formatLocalDateTime(createdAt)}
      >
        {formatLocalClockTime(createdAt)}
      </span>
      <span
        aria-hidden
        className={clsx(
          'flex size-6 shrink-0 items-center justify-center rounded-full text-[13px] font-bold',
          resolved
            ? SENDER_TONE_CLASS[senderTone(authorId)]
            : // An unresolved sender gets the neutral chip rather than a colour
              // that would imply an identity the server could not confirm.
              'bg-foreground/[0.06] text-muted',
        )}
      >
        {resolved ? senderInitial(resolved) : '?'}
      </span>
    </div>
  );
};
