import { Check, MapPin, MessageSquareQuote } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

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

// What the card actually draws, distilled from ``content.annotation``.
//
// ``action`` collapses to a boolean deliberately. Rule 07: only ``resolved``
// draws a marker; created / updated / dismissed draw nothing beyond the body.
// Not carrying the other three past the parse means no later edit can quietly
// start branching on them without going back to the design first.
export type AnnotationView = {
  direction: 'user' | 'agent';
  resolved: boolean;
  quote?: string;
};

// Reads ``content.annotation`` off a chat row; null when the row carries no
// usable display record.
//
// ``direction`` is the only thing that decides the side and the title — rule 01.
// A forward annotation is authored by ``harness`` (it is turn input), so reading
// ``author`` here would put the user's own annotation on the left behind a
// harness chip. ``author`` is bookkeeping and never reaches the view.
export function readAnnotationView(content: unknown): AnnotationView | null {
  const raw = (content as { annotation?: unknown } | null | undefined)?.annotation;
  if (!raw || typeof raw !== 'object') return null;
  const { direction, action, quote } = raw as Record<string, unknown>;
  if (direction !== 'user' && direction !== 'agent') return null;
  return {
    direction,
    resolved: action === 'resolved',
    // Rule 04: the strip needs copy the reader can find on the page. An empty
    // string is the same as no quote.
    quote: typeof quote === 'string' && quote.trim().length > 0 ? quote : undefined,
  };
}

export const annotationTitleKey = (direction: AnnotationView['direction']): string =>
  direction === 'user' ? 'chat.annotation.titleUser' : 'chat.annotation.titleAgent';

// Whether a transcript row is drawn as the annotation card — the transcript's
// claim check, in one place so the row branch and its test exercise the same
// code.
//
// ``type`` decides, and nothing else: not ``author`` (a forward annotation is
// authored by ``harness``), not ``source``, not ``metadata.source`` (a forward
// annotation carries ``show_page`` in every state, including while queued). The
// display record only narrows the claim further, so a row that somehow arrives
// without a readable ``direction`` degrades to an ordinary bubble instead of a
// card that cannot say whose annotation it is.
export function claimAnnotation(message: { type: string; content?: unknown }): AnnotationView | null {
  return message.type === 'annotation' ? readAnnotationView(message.content) : null;
}

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
        <div
          className={clsx(
            // Rule 03: width, radius and timestamp are the existing chat bubble's.
            // An annotation is a message, not a widget — it must not introduce a
            // second bubble shape into the column.
            'w-fit min-w-0 max-w-full rounded-2xl border px-3.5 py-2.5 leading-relaxed [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_table]:w-full',
            fromUser
              ? 'rounded-tr-md border-border-strong bg-foreground/[0.06]'
              : 'rounded-tl-md border-mint/25 bg-mint/[0.09]',
          )}
          style={bodyStyle}
        >
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
