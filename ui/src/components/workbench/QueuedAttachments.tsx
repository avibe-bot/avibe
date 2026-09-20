import * as React from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ChevronDown, FileText, Image as ImageIcon, ImageOff } from 'lucide-react';

import { useFileViewer } from '@/components/ui/file-viewer-context';
import { useImageViewer } from '@/components/ui/image-viewer-context';
import { isProxyMediaUrl } from '@/lib/mediaProxy';
import type { MessageAttachment } from '@/lib/messageAttachments';

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
    (att: MessageAttachment, unavailable: boolean) => {
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

const Thumb: React.FC<{ att: MessageAttachment }> = ({ att }) => {
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

const Chip: React.FC<{ att: MessageAttachment }> = ({ att }) => {
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
  if (!isProxyMediaUrl(att.url)) {
    // A third-party URL is opened the way a delivered attachment opens one: the
    // `FileCard`'s plain external link. The in-app viewer deliberately refuses
    // to fetch a host that is not ours, so routing this there would reach a
    // modal that can only ever say "preview failed" — the name would be
    // readable, but the file would not.
    return (
      <a
        href={att.url}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={name}
        title={name}
        data-queue-attachment="file-external"
        className={TARGET}
      >
        <span className={pill}>{body}</span>
      </a>
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

const Item: React.FC<{ att: MessageAttachment }> = ({ att }) =>
  att.image ? <Thumb att={att} /> : <Chip att={att} />;

const Disclosure: React.FC<{
  count: number;
  expanded: boolean;
  onToggle: () => void;
}> = ({ count, expanded, onToggle }) => {
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
      className={TARGET}
    >
      <span className={clsx(PILL, 'border-border-strong bg-muted-soft font-mono text-[10px] font-bold')}>
        {!expanded && t('chat.queue.attachments.more', { count })}
        <ChevronDown className={clsx('size-[9px] shrink-0', expanded && 'rotate-180')} aria-hidden="true" />
      </span>
    </button>
  );
};

// How many attachments fit on the collapsed line: three at a desktop width, two
// below `sm`.
//
// This is one number, read once, rather than two mirrored sets of classes. The
// CSS version made a *layout* own focus: one logical disclosure existed as two
// elements (`sm:hidden` / `hidden sm:flex`) and the third slot as a
// `hidden sm:flex` wrapper, so crossing the breakpoint turned whichever one held
// focus into `display:none`. The browser blurs such an element and CSS cannot
// hand focus to its replacement, so focus fell to the document — the same defect
// as unmounting a focused button, reached by a different route.
//
// This is a media-query *change* listener, not element measurement: it fires
// twice per breakpoint crossing for the whole page, reads no geometry, and does
// not grow with the number of rows in the strip.
const WIDE_QUERY = '(min-width: 640px)'; // the `sm` token
const INLINE_CAPACITY = { narrow: 2, wide: 3 };

const readWide = () =>
  typeof window !== 'undefined' && Boolean(window.matchMedia?.(WIDE_QUERY).matches);

// The inline group, between the row's text and its actions.
//
// Exactly one disclosure button exists per row, and it is the same DOM element
// for as long as a disclosure action is available at all — across expand,
// collapse and breakpoint changes alike. When it stops being available, focus is
// handed to `onFocusEscape` rather than left on a hidden node or dropped.
export const QueuedAttachmentGroup: React.FC<{
  attachments: MessageAttachment[];
  expanded: boolean;
  onToggle: () => void;
  /**
   * Where focus goes when this group loses the element that was holding it — the
   * row's own text control. Only ever called when focus was inside this group
   * immediately before the change that removed it.
   */
  onFocusEscape?: () => void;
}> = ({ attachments, expanded, onToggle, onFocusEscape }) => {
  const root = React.useRef<HTMLDivElement>(null);
  // Set immediately before a change that may remove the focused node, read once
  // the new DOM is committed. Nothing else sets it, so a group that did not own
  // focus can never take it from whoever does.
  const held = React.useRef(false);
  const hold = React.useCallback(() => {
    const active = document.activeElement;
    held.current = Boolean(active && root.current?.contains(active));
  }, []);

  // `hold()` runs inside the listener, while the DOM that is about to change is
  // still the one on screen. That is the only moment at which "who has focus
  // right now" is still answerable — by the time React has committed, a removed
  // element has already taken focus to the document with it.
  const [wide, setWide] = React.useState(readWide);
  React.useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia(WIDE_QUERY);
    // Reconcile anything that moved between the first render and this effect.
    // No focus bookkeeping: nothing was removed under a user yet.
    setWide(media.matches);
    const onChange = (event: MediaQueryListEvent) => {
      hold();
      setWide(event.matches);
    };
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, [hold]);
  const capacity = wide ? INLINE_CAPACITY.wide : INLINE_CAPACITY.narrow;

  React.useLayoutEffect(() => {
    if (!held.current) return;
    held.current = false;
    // The element survived the change, so the browser kept focus on it and there
    // is nothing to move.
    if (root.current?.contains(document.activeElement)) return;
    onFocusEscape?.();
  });

  const total = attachments.length;
  const hiddenCount = Math.max(0, total - capacity);
  if (total === 0) return null;
  return (
    <div
      ref={root}
      className="flex shrink-0 items-center gap-1"
      data-queue-attachments={expanded ? 'expanded' : 'inline'}
    >
      {/* Disclosing replaces the inline previews rather than adding to them: the
          sheet below lists every attachment exactly once, in source order. */}
      {!expanded && attachments.slice(0, capacity).map((att, i) => <Item key={i} att={att} />)}
      {/* Rendered whenever there is something to disclose or something disclosed,
          always in this position, so the button a keyboard user is standing on
          keeps its identity through the toggle and through a resize. */}
      {(expanded || hiddenCount > 0) && (
        <Disclosure
          count={hiddenCount}
          expanded={expanded}
          onToggle={() => {
            hold();
            onToggle();
          }}
        />
      )}
    </div>
  );
};

// The disclosed remainder: the same row wrapped onto a second line, not a second
// strip — ordering, identity and the row's own actions are untouched, and the
// queue's existing 128px scroll body absorbs the extra height.
//
// The row gap is 12px, not the 4px used between neighbours on one line, and the
// difference is a hit-testing fact rather than a spacing preference: `TARGET`
// extends each 24px band by 6px above and below, so two stacked targets claim
// 12px of vertical space between their bands. Give them 4px and the extensions
// overlap by 8px — measured at 390px, a tap three pixels below one thumbnail
// opened the file on the next line. At 12px they meet exactly and never cross,
// which is the same geometry the disclosure-to-sheet boundary already has.
export const QueuedAttachmentSheet: React.FC<{ attachments: MessageAttachment[] }> = ({ attachments }) => (
  <div
    data-queue-attachments="disclosed"
    className="order-last mt-1 flex w-full basis-full flex-wrap items-center gap-x-1 gap-y-3"
  >
    {attachments.map((att, i) => (
      <Item key={i} att={att} />
    ))}
  </div>
);
