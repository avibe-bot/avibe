import { ArrowLeft } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { Skeleton, SkeletonShimmer } from '../ui/skeleton';

// A conversation opened before its messages arrived (ui/design: chat/skeleton/*).
// What the page already knows is drawn as itself; only what it does not know is
// blocks. The transcript's blocks are the bubbles it will show: the user's on the
// right, the Agent's on the left under its avatar and name, each with the time
// line the transcript keeps under a bubble. Bubble sizes are fractions of the
// column, capped on the spacing scale; the bubble never exceeds
// `--chat-message-max-w`, as a real one does not.

type BubbleShape = { side: 'user' | 'agent'; width: string; lines: string[] };

// A conversation the page knows nothing of: a short exchange, top down.
const OPENING: BubbleShape[] = [
  { side: 'user', width: 'w-18/25 max-w-88', lines: ['w-full', 'w-29/50'] },
  { side: 'agent', width: 'w-22/25 max-w-176', lines: ['w-full', 'w-47/50', 'w-43/50', 'w-12/25'] },
  { side: 'user', width: 'w-13/25 max-w-56', lines: ['w-22/25'] },
  { side: 'agent', width: 'w-21/25 max-w-152', lines: ['w-full', 'w-9/10', 'w-2/5'] },
];
// Above a known latest message: the older ones, the newest of them last.
const OLDER: BubbleShape[] = [
  { side: 'agent', width: 'w-43/50 max-w-160', lines: ['w-full', 'w-23/25', 'w-27/50'] },
  { side: 'user', width: 'w-16/25 max-w-72', lines: ['w-full', 'w-2/5'] },
  { side: 'agent', width: 'w-41/50 max-w-144', lines: ['w-full', 'w-7/10'] },
  { side: 'user', width: 'w-7/10 max-w-80', lines: ['w-21/25'] },
];

function BubbleSkeleton({ side, width, lines }: BubbleShape) {
  return (
    <div className={clsx('flex w-full', side === 'user' ? 'justify-end' : 'justify-start')}>
      <div
        className={clsx(
          'flex max-w-(--chat-message-max-w) flex-col gap-1',
          side === 'user' ? 'items-end' : 'items-start',
          width,
        )}
      >
        {side === 'agent' && (
          <div className="flex items-center gap-2 px-0.5">
            <Skeleton className="size-6 rounded-full" />
            <span className="flex h-[1lh] items-center text-[11px]">
              <Skeleton className="h-1.5 w-10" />
            </span>
          </div>
        )}
        <div
          className={clsx(
            'flex w-full flex-col rounded-2xl border border-transparent bg-foreground/[0.04] px-3.5 py-2.5 text-[14px] leading-[1.6]',
            side === 'user' ? 'rounded-tr-md' : 'rounded-tl-md',
          )}
        >
          {lines.map((line, index) => (
            <span key={index} className="flex h-[1lh] items-center">
              <Skeleton className={clsx('h-2', line)} />
            </span>
          ))}
        </div>
        {/* The time line under each bubble: shown on touch screens, kept as space elsewhere. */}
        <span className="flex h-[1lh] items-center px-1 font-mono text-[10px]">
          <Skeleton className="h-1.5 w-16 opacity-0 pointer-coarse:opacity-100" />
        </span>
      </div>
    </div>
  );
}

/** The transcript's messages still to come: an opening exchange, or, above a
 *  latest message the page already shows (`older`), the ones before it. */
export function TranscriptSkeleton({ older = false }: { older?: boolean }) {
  const { t } = useTranslation();
  return (
    <SkeletonShimmer label={t('common.loading')} className="flex w-full flex-col gap-3">
      {(older ? OLDER : OPENING).map((bubble, index) => (
        <BubbleSkeleton key={index} {...bubble} />
      ))}
    </SkeletonShimmer>
  );
}

/** A conversation the page knows nothing of yet, not even its row: the header's
 *  way back is real, its title is a block, the transcript is an opening exchange
 *  of blocks, and the composer is a block of its shape. */
export function ChatPageSkeleton({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  return (
    // The chat page's own frame (ChatPage), so the page that replaces this one
    // fills exactly the same box.
    <div
      data-chat-skeleton="page"
      className="fixed inset-x-0 bottom-0 top-[var(--macos-stale-login-session-notice-h,0px)] z-40 flex flex-col bg-background md:relative md:inset-auto md:z-auto md:-mx-10 md:-my-8 md:h-[var(--app-vvh)] md:bg-transparent"
    >
      {/* The header bar as ChatHeaderBar draws it: the way back, and the title's place. */}
      <div className="h-[calc(3rem+env(safe-area-inset-top))] shrink-0 border-b border-border bg-background/92 px-4 pt-[env(safe-area-inset-top)] backdrop-blur md:h-auto md:bg-surface/70 md:px-8 md:py-2.5 md:pt-2.5">
        <div className="mx-auto flex h-full w-full max-w-(--chat-column-w) items-center gap-3 md:h-auto">
          <Button type="button" variant="outline" size="icon" onClick={onBack} aria-label={t('chat.back')} className="size-7 shrink-0">
            <ArrowLeft className="size-3.5" />
          </Button>
          <span className="flex h-7 items-center text-[15px]">
            <Skeleton className="h-2.5 w-40" />
          </span>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden px-4 py-5 md:px-8">
        <div className="mx-auto flex w-full max-w-(--chat-column-w) flex-col gap-3">
          <TranscriptSkeleton />
        </div>
      </div>
      <div className="shrink-0 px-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-3 md:px-8 md:pb-4">
        <div className="mx-auto w-full max-w-(--chat-column-w)">
          <Skeleton className="h-[104px] w-full rounded-2xl" />
        </div>
      </div>
    </div>
  );
}
