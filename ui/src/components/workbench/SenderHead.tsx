import clsx from 'clsx';
import { UserRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
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
  const { readerPrincipal } = useInstanceAuthorization();
  // A row written by the reader's own principal is theirs: "You" over the
  // default avatar, whether they sit at the loopback or come in through Cloud.
  // Anyone else's ``local`` row still has no email behind it and reads as
  // "unknown" -- "You" would attribute it to the wrong person.
  const own = Boolean(readerPrincipal) && authorId === readerPrincipal;
  // No label means the server could not resolve this principal — an IM-relayed
  // human row, or a subject Cloud never stored an email for. Naming it anything
  // but "unknown" would attribute the message to someone.
  const resolved = own ? null : typeof label === 'string' && label.trim() ? label.trim() : null;

  return (
    <div className="flex max-w-full items-center gap-2.5 px-1">
      <span className="truncate text-[13px] font-bold text-foreground">
        {own ? t('chat.senderYou') : (resolved ?? t('chat.senderUnknown'))}
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
            : // The reader's own row and an unresolved sender both get the
              // neutral chip: neither has a server-confirmed label to colour.
              'bg-foreground/[0.06] text-muted',
        )}
      >
        {own ? <UserRound className="size-3.5" /> : resolved ? senderInitial(resolved) : '?'}
      </span>
    </div>
  );
};
