import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useInRouterContext } from 'react-router-dom';

import { useLiveSessionTitle } from '../../lib/liveSessionTitles';
import { cn } from '../../lib/utils';
import { badgeVariants, inlineBadgeTriggerClassName } from './badge-variants';

export type SessionMentionChipProps = {
  sessionId: string;
  /** Resolved Session title from the server (`null` when the Session exists and
   *  is openable, but has no title yet; `undefined` on legacy composer sidecars
   *  without live enrichment). */
  title?: string | null;
  /** Fallback ReactNode for legacy composer sidecars. */
  fallback?: ReactNode;
  /** When true, clicking/tapping navigates to `/chat/<sessionId>`. Set false
   *  inside non-interactive previews or for non-openable legacy chips. */
  interactive?: boolean;
  className?: string;
};

/** A `#<session-id>` mention as a chip showing the Session's current title and
 *  opening that Session on click/tap. Long titles truncate with an ellipsis
 *  inside the chip; `leading-none` and optical baseline alignment keep the
 *  surrounding prose line height unchanged. */
export const SessionMentionChip: React.FC<SessionMentionChipProps> = ({
  sessionId,
  title,
  fallback,
  interactive = true,
  className,
}) => {
  const { t } = useTranslation();
  const inRouter = useInRouterContext();
  const liveTitle = useLiveSessionTitle(sessionId);

  const trimmedLive = liveTitle?.trim() || '';
  const trimmedProp = title?.trim() || '';
  let labelText: string | null = null;
  if (trimmedLive) {
    labelText = trimmedLive;
  } else if (liveTitle !== undefined) {
    // Bot Sessions (`ses_<handle>`) store `title: null` in SQLite; the server
    // supplies the Bot's display name in `title`, so a null SSE title must not
    // replace it with "Untitled session".
    labelText = sessionId.startsWith('ses_') && trimmedProp
      ? trimmedProp
      : t('workbench.untitledSession');
  } else if (title !== undefined) {
    labelText = trimmedProp || t('workbench.untitledSession');
  }

  const displayNode = labelText !== null ? t('workbench.sessionMentionName', { title: labelText }) : fallback;
  const tooltip = labelText ?? undefined;

  const chipClassName = cn(
    badgeVariants({ variant: 'info' }),
    'vr-mention-chip max-w-[min(16rem,100%)] py-[2px] align-[-0.18em] font-medium leading-none',
    interactive && inlineBadgeTriggerClassName,
    interactive && 'hover:bg-cyan/20 focus-visible:ring-2 focus-visible:ring-ring',
    className,
  );

  const inner = <span className="min-w-0 truncate">{displayNode}</span>;

  if (!interactive) {
    return (
      <span className={chipClassName} title={tooltip} data-session-mention={sessionId}>
        {inner}
      </span>
    );
  }

  const href = `/chat/${encodeURIComponent(sessionId)}`;
  if (inRouter) {
    return (
      <Link to={href} className={chipClassName} title={tooltip} data-session-mention={sessionId}>
        {inner}
      </Link>
    );
  }

  return (
    <a href={href} className={chipClassName} title={tooltip} data-session-mention={sessionId}>
      {inner}
    </a>
  );
};
