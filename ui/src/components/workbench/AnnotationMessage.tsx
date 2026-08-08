import { Check, MapPin, MessageSquareQuote } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { annotationTitleKey, type AnnotationView } from '../../lib/annotationView';
import { AGENT_BUBBLE, USER_BUBBLE } from './chatBubble';
import { RoleAvatar } from './RoleAvatar';

// A Show Page annotation, in chat. Both directions are the same message type —
// the user annotating the page, and the agent marking it back — and the card's
// title is what says which. Design: design.pen m31JWV (states) + TxFKk (anatomy
// and rules); the rule numbers cited below are that frame's.
//
// The card is a row component, not a page fragment: it takes the body,
// attachment and timestamp nodes the transcript already builds and only adds
// what is specific to an annotation (title, anchor quote, resolved marker), so
// it cannot drift from the surrounding bubbles.

type AnnotationMessageProps = {
  messageId: string;
  view: AnnotationView;
  /** The transcript's own Markdown body node. */
  body: React.ReactNode;
  /** The transcript's own attachment renderer — rule 06: the screenshot reuses
   *  the existing thumbnail, viewer modal and proxy-url guard, with no second
   *  image element anywhere in the codebase. */
  attachments: React.ReactNode;
  /** The transcript's own hover-revealed timestamp — rule 03. */
  time: React.ReactNode;
  bodyStyle?: React.CSSProperties;
  /** The transcript's row dressing (deep-link highlight); the card supplies only
   *  the alignment, because the alignment is the part that depends on direction. */
  rowClass: (extra: string) => string;
};

export const AnnotationMessage: React.FC<AnnotationMessageProps> = ({
  messageId,
  view,
  body,
  attachments,
  time,
  bodyStyle,
  rowClass,
}) => {
  const { t } = useTranslation();
  const fromUser = view.direction === 'user';

  // Rule 02: two title values, and the action never enters the title. A resolved
  // agent mark is still titled "Agent 批注" — the marker below says it is done.
  const label = (
    <span className={clsx('text-[11px] font-medium', fromUser ? 'text-cyan' : 'text-mint')}>
      {t(annotationTitleKey(view.direction))}
    </span>
  );
  const avatar = (
    <RoleAvatar tone={fromUser ? 'cyan' : 'mint'}>
      <MessageSquareQuote />
    </RoleAvatar>
  );

  // Rule 04: drawn only when the anchor carries copy a reader can find on the
  // page. When it does not, nothing is drawn — a selector, an anchor kind, an
  // event id or a screenshot path is a locator the reader cannot use, so none of
  // them stands in for the quote (rule 05).
  const quoteNode = view.quote ? (
    <div className="mb-[9px] flex w-full items-start gap-[7px] border-l-2 border-foreground/25 py-px pl-[9px]">
      <span className="pt-[3px]">
        <MapPin className="size-[11px] shrink-0 text-muted" />
      </span>
      <span className="min-w-0 break-words text-[12px] leading-[1.5] text-muted">{view.quote}</span>
    </div>
  ) : null;

  // Rule 07.
  const resolvedNode = view.resolved ? (
    <div className="mt-[9px] flex w-full">
      <span className="inline-flex items-center gap-1 rounded-full border border-mint/35 bg-mint-soft px-[9px] py-[3px] text-[10.5px] font-semibold text-mint">
        <Check className="size-[11px] shrink-0" />
        {t('chat.annotation.resolved')}
      </span>
    </div>
  ) : null;

  return (
    <div data-message-id={messageId} className={rowClass(fromUser ? 'justify-end' : 'justify-start')}>
      <div
        className={clsx(
          'group/message flex max-w-[min(92%,860px)] flex-col gap-1',
          fromUser ? 'items-end' : 'items-start',
        )}
      >
        {/* Mirrored head: the avatar always sits on the outer edge, so the two
            directions read as a matched pair across the column. */}
        <div className="flex items-center gap-2 px-0.5">
          {fromUser ? (
            <>
              {label}
              {avatar}
            </>
          ) : (
            <>
              {avatar}
              {label}
            </>
          )}
        </div>
        {/* Rule 03: the bubble is the column's existing bubble, not a new shape. */}
        <div className={fromUser ? USER_BUBBLE : AGENT_BUBBLE} style={bodyStyle}>
          {quoteNode}
          {body}
          {attachments}
          {resolvedNode}
        </div>
        {time}
      </div>
    </div>
  );
};
