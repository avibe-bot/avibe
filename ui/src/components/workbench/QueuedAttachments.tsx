import * as React from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ChevronDown, FileText, Image as ImageIcon, ImageOff } from 'lucide-react';

import { useFileViewer } from '@/components/ui/file-viewer-context';
import { useImageViewer } from '@/components/ui/image-viewer-context';
import type { QueuedAttachment } from '@/lib/queuedAttachments';

// What a queued message's attachments look like on the one line the queue strip
// gives them. Design: avibe-docs `design/queued-message-attachments` QMA 01–04;
// contract: docs/plans/2026-09-19-queued-attachment-previews.md.
//
// Why this is not `ChatImage` / `FileCard`: those two are the *transcript's*
// attachment treatment — a 352x240 image and a 240px-wide card. A queue row is
// 36px tall and has to stay 36px tall, so the recognition unit here is a 20x20
// thumbnail and a 20px single-line chip. What is shared with the transcript is
// the part that matters for correctness: the same same-origin proxy test
// deciding what may be fetched at all, and the same two viewers for inspecting
// a file.

// "console-log.png · PNG", plus "· Preview unavailable" once an image is known
// not to render. The name is data and the state is i18n, joined the way the file
// viewer's own meta line joins them.
const useAccessibleName = () => {
  const { t } = useTranslation();
  return React.useCallback(
    (att: QueuedAttachment, unavailable: boolean) => {
      const name = att.name || t('chat.queue.attachments.untitled');
      const ext = att.name.includes('.') ? (att.name.split('.').pop() || '').toUpperCase() : '';
      return [name, ext || null, unavailable ? t('chat.queue.attachments.previewUnavailable') : null]
        .filter(Boolean)
        .join(' · ');
    },
    [t],
  );
};

// Every target sits in the same 24px band the row's icon buttons already use, so
// no attachment — of any type, in any load state — can make a collapsed row
// taller than a text-only one. The visual inside it is 20px; the band plus
// ``after`` grows the hit area to 24x36 CSS px, which consumes exactly the 4px
// gap to each neighbour without overlapping it.
const TARGET =
  'relative flex h-6 shrink-0 items-center rounded-[5px] text-muted transition-colors hover:text-foreground ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan/40 ' +
  "after:absolute after:-inset-x-0.5 after:-inset-y-1.5 after:content-['']";

const PILL = 'flex h-5 items-center gap-1 rounded-[5px] border px-1';

const Thumb: React.FC<{ att: QueuedAttachment }> = ({ att }) => {
  const label = useAccessibleName();
  const imageViewer = useImageViewer();
  const fileViewer = useFileViewer();
  // One state, one meaning: the browser told us this src does not render. The
  // slot keeps its geometry either way; only the glyph and the name change.
  const [failed, setFailed] = React.useState(false);
  const name = label(att, failed);
  return (
    <button
      type="button"
      // A failed thumbnail hands its click to the file viewer instead: that is
      // where the complete filename is readable without hovering, which is the
      // only place left to read it once the picture is gone.
      onClick={() =>
        failed
          ? fileViewer?.open({ url: att.url, name: att.name })
          : imageViewer?.open(att.url, { isolated: true })
      }
      aria-label={name}
      title={name}
      data-queue-attachment={failed ? 'image-unavailable' : 'image'}
      className={TARGET}
    >
      <span className="relative flex size-5 items-center justify-center overflow-hidden rounded-[4px] border border-border bg-surface-3">
        {/* Under the picture, so the slot is never empty: this is what shows
            while the image is still loading and what remains when it fails. */}
        {failed ? (
          <ImageOff className="size-[11px]" aria-hidden="true" />
        ) : (
          <ImageIcon className="size-[11px]" aria-hidden="true" />
        )}
        {!failed && (
          <img
            src={att.url}
            alt=""
            loading="lazy"
            decoding="async"
            onError={() => setFailed(true)}
            className="absolute inset-0 size-full object-cover"
          />
        )}
      </span>
    </button>
  );
};

const Chip: React.FC<{ att: QueuedAttachment }> = ({ att }) => {
  const { t } = useTranslation();
  const label = useAccessibleName();
  const fileViewer = useFileViewer();
  const name = label(att, false);
  const pill = clsx(PILL, 'border-border bg-surface-3 text-[10.5px]');
  const body = (
    <>
      <FileText className="size-[11px] shrink-0" aria-hidden="true" />
      {/* A narrow layout shrinks the name back towards the type glyph rather
          than taking width from the text and the row's actions. The complete
          name is never hover-only: the viewer this chip opens puts it in its
          title, including for a type it cannot render and a URL it won't fetch. */}
      <span className="max-w-[52px] truncate sm:max-w-[88px]">
        {att.name || t('chat.queue.attachments.untitled')}
      </span>
    </>
  );
  if (!att.url) {
    // Nothing to open. The name still belongs on the row, so it is shown rather
    // than put behind an action that cannot work.
    return (
      <span data-queue-attachment="file-inert" title={name} className={clsx(TARGET, 'after:hidden')}>
        <span className={pill}>{body}</span>
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => fileViewer?.open({ url: att.url, name: att.name })}
      aria-label={name}
      title={name}
      data-queue-attachment="file"
      className={TARGET}
    >
      <span className={pill}>{body}</span>
    </button>
  );
};

const Item: React.FC<{ att: QueuedAttachment }> = ({ att }) =>
  att.image ? <Thumb att={att} /> : <Chip att={att} />;

const Disclosure: React.FC<{
  count: number;
  expanded: boolean;
  onToggle: () => void;
  className?: string;
}> = ({ count, expanded, onToggle, className }) => {
  const { t } = useTranslation();
  const label = expanded
    ? t('chat.queue.attachments.collapse')
    : t('chat.queue.attachments.showMore', { count });
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      aria-label={label}
      title={label}
      data-queue-attachment-more="true"
      className={clsx(TARGET, className)}
    >
      <span className={clsx(PILL, 'border-border-strong bg-muted-soft font-mono text-[10px] font-bold')}>
        {!expanded && t('chat.queue.attachments.more', { count })}
        <ChevronDown className={clsx('size-[9px] shrink-0', expanded && 'rotate-180')} aria-hidden="true" />
      </span>
    </button>
  );
};

// The inline group, between the row's text and its actions.
//
// Capacity is chosen in CSS, not in JS: three attachments at a desktop width and
// two below `sm`, with a `+N` per breakpoint so the number always matches what is
// actually hidden. A resize listener would have to re-measure every row inside a
// scrolling strip, and would make the server-rendered markup a third state that
// matches neither width.
export const QueuedAttachmentGroup: React.FC<{
  attachments: QueuedAttachment[];
  expanded: boolean;
  onToggle: () => void;
}> = ({ attachments, expanded, onToggle }) => {
  const total = attachments.length;
  if (total === 0) return null;
  if (expanded) {
    return (
      <div className="flex shrink-0 items-center" data-queue-attachments="expanded">
        <Disclosure count={total} expanded onToggle={onToggle} />
      </div>
    );
  }
  return (
    <div className="flex shrink-0 items-center gap-1" data-queue-attachments="inline">
      {attachments.slice(0, 2).map((att, i) => (
        <Item key={i} att={att} />
      ))}
      {total > 2 && (
        // The third slot is desktop-only, and `hidden` is display:none — so at a
        // narrow width it leaves the tab order with its thumbnail, and exactly
        // one of the two `+N` buttons below is reachable at any one width.
        <div className="hidden shrink-0 sm:flex">
          <Item att={attachments[2]} />
        </div>
      )}
      {total > 2 && (
        <Disclosure count={total - 2} expanded={false} onToggle={onToggle} className="sm:hidden" />
      )}
      {total > 3 && (
        <Disclosure count={total - 3} expanded={false} onToggle={onToggle} className="hidden sm:flex" />
      )}
    </div>
  );
};

// The disclosed remainder: the same row wrapped onto a second line, not a second
// strip — ordering, identity and the row's own actions are untouched, and the
// queue's existing 128px scroll body absorbs the extra height.
export const QueuedAttachmentSheet: React.FC<{ attachments: QueuedAttachment[] }> = ({ attachments }) => (
  <div
    data-queue-attachments="disclosed"
    className="order-last mt-1 flex w-full basis-full flex-wrap items-center gap-1"
  >
    {attachments.map((att, i) => (
      <Item key={i} att={att} />
    ))}
  </div>
);
