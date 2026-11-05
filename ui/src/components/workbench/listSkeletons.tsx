import type { ReactNode } from 'react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Skeleton, SkeletonShimmer } from '../ui/skeleton';
import { useSkeletonHandover } from '../ui/skeleton-handover';
import { InboxSectionHeader } from './InboxSectionHeader';
import { SESSION_ROW_INDENT_CLASS } from './sessionRowLayout';

// The skeletons of the main lists (ui/design: system/skeleton/*). Each row copies
// the box of the row it stands in for (padding, gap, the icon's size, and a text
// line one line tall at the row's text size), so the rows that replace it land on
// the same pixels. Titles vary in length the way real ones do; the widths repeat
// in this order so every render of a list looks the same.
const TITLE_WIDTHS = ['w-[72%]', 'w-[52%]', 'w-[64%]', 'w-[44%]', 'w-[58%]'];
const titleWidth = (index: number) => TITLE_WIDTHS[index % TITLE_WIDTHS.length];

/** A line of text at `textClass`'s size: a box one line tall with a bar inside. */
const TextLine = ({ textClass, barClass, className }: { textClass: string; barClass: string; className?: string }) => (
  <span className={clsx('flex h-[1lh] min-w-0 items-center', textClass, className)}>
    <Skeleton className={barClass} />
  </span>
);

/** Session rows under an expanded project in the desktop sidebar (`SessionRow`). */
export const SidebarSessionRowsSkeleton = ({ rows }: { rows: number }) => {
  const { t } = useTranslation();
  return (
    <SkeletonShimmer label={t('workbench.sessionsLoading')} className="flex flex-col gap-0.5 rounded-md">
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className={clsx('flex items-center gap-2 border-transparent py-1.5 pr-2.5', SESSION_ROW_INDENT_CLASS)}
        >
          <Skeleton className="size-[5px] rounded-full" />
          <TextLine textClass="flex-1 text-[12px]" barClass={clsx('h-2', titleWidth(index))} />
        </div>
      ))}
    </SkeletonShimmer>
  );
};

/** Project rows in the desktop sidebar before the first list arrives (`ProjectRow`'s header). */
export const SidebarProjectRowsSkeleton = ({ rows }: { rows: number }) => {
  const { t } = useTranslation();
  return (
    <SkeletonShimmer label={t('workbench.projectsLoading')} className="flex flex-col gap-0.5 rounded-md">
      {Array.from({ length: rows }, (_, index) => (
        // The real row also holds its 20px ⋯ and ＋ buttons, which set its height.
        <div key={index} className="flex h-8 items-center gap-1.5 py-1.5 pl-[13px] pr-2">
          <Skeleton className="size-3.5 rounded-[3px]" />
          <TextLine textClass="flex-1 text-[12px]" barClass={clsx('h-2', titleWidth(index + 1))} />
        </div>
      ))}
    </SkeletonShimmer>
  );
};

/** Session rows inside an open project card on the phone's Projects page (`MobileSessionRow`). */
export const MobileSessionRowsSkeleton = ({ rows }: { rows: number }) => {
  const { t } = useTranslation();
  return (
    <SkeletonShimmer label={t('common.loading')} className="flex flex-col gap-0.5 rounded-lg">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex items-center gap-2.5 px-3 py-2.5">
          <Skeleton className="size-1.5 rounded-full" />
          <TextLine textClass="flex-1 text-[13px]" barClass={clsx('h-2.5', titleWidth(index))} />
          <TextLine textClass="text-[10.5px]" barClass="h-2 w-7" />
        </div>
      ))}
    </SkeletonShimmer>
  );
};

/** Project cards on the phone's Projects page before the first list arrives (`MobileProjectRow`).
 *  `quiet` inside a larger skeleton that already announces itself. */
export const MobileProjectCardsSkeleton = ({ rows, quiet = false }: { rows: number; quiet?: boolean }) => {
  const { t } = useTranslation();
  return (
    <div role={quiet ? undefined : 'status'} aria-busy={quiet ? undefined : true} className="flex flex-col gap-2">
      {!quiet && <span className="sr-only">{t('common.loading')}</span>}
      {Array.from({ length: rows }, (_, index) => (
        <SkeletonShimmer key={index} className="rounded-xl border border-border bg-surface">
          <div className="flex items-center pr-1.5">
            <div className="flex min-w-0 flex-1 items-center gap-2.5 px-4 py-3.5">
              <Skeleton className="size-4 rounded-[4px]" />
              <TextLine textClass="flex-1 text-sm" barClass={clsx('h-2.5', titleWidth(index + 2))} />
            </div>
          </div>
        </SkeletonShimmer>
      ))}
    </div>
  );
};

/** One Inbox card (`InboxPage`'s card), drawn at the most common real card: an unread one
 *  (Open and Mark read), whose preview runs two lines on a phone and one on a wider screen.
 *  A Bot's card leads with its 24px avatar, a Session's with its project tag. */
const InboxCardSkeleton = ({ kind, index }: { kind: 'bot' | 'session'; index: number }) => (
  <SkeletonShimmer className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-4">
    <div className={clsx('flex items-center gap-2 text-[11px]', kind === 'bot' && 'h-6')}>
      {kind === 'bot' ? (
        <Skeleton className="size-6 rounded-full" />
      ) : (
        <Skeleton className="border border-transparent px-2 py-0.5">
          <span className="block h-[1lh] w-12" />
        </Skeleton>
      )}
      <TextLine textClass="flex-1 text-[13px]" barClass={clsx('h-2.5', titleWidth(index))} />
      <TextLine textClass="font-mono" barClass="h-2 w-10" />
    </div>
    <div className="flex flex-col gap-1">
      <TextLine textClass="text-[10px]" barClass="h-1.5 w-10" />
      <div className="flex flex-col text-[13px] leading-relaxed">
        <TextLine textClass="" barClass={clsx('h-2', index % 2 ? 'w-full md:w-[46%]' : 'w-full md:w-[78%]')} />
        <TextLine textClass="md:hidden" barClass={clsx('h-2', index % 2 ? 'w-[46%]' : 'w-[70%]')} />
      </div>
    </div>
    <div className="flex items-center gap-2">
      <Skeleton className="border border-transparent px-3 py-1.5 text-[11px]">
        <span className="block h-[1lh] w-20" />
      </Skeleton>
      <Skeleton className="border border-transparent px-3 py-1.5 text-[11px]">
        <span className="block h-[1lh] w-14" />
      </Skeleton>
    </div>
  </SkeletonShimmer>
);

/** Conversation cards on the Inbox page before the first page arrives. With Bots the
 *  page files them in two sections, Bots first, so the skeleton does too: their heads
 *  are known and drawn as themselves (the Sessions head with the room its "mark this
 *  group read" takes), one Bot card and two Session cards under them. */
export const InboxCardsSkeleton = ({ rows, sections = false }: { rows: number; sections?: boolean }) => {
  const { t } = useTranslation();
  return (
    <div role="status" aria-busy="true" className="flex flex-col gap-3">
      <span className="sr-only">{t('common.loading')}</span>
      {sections ? (
        <>
          <section className="flex flex-col gap-3">
            <InboxSectionHeader category="bot" unread={0} quiet={false} className="px-1 pt-1" />
            <InboxCardSkeleton kind="bot" index={0} />
          </section>
          <section className="flex flex-col gap-3">
            <InboxSectionHeader
              category="session"
              unread={0}
              quiet={false}
              className="px-1 pt-1"
              action={
                <Skeleton className="border border-transparent px-2 py-0.5 text-[11px]">
                  <span className="block h-[1lh] w-16" />
                </Skeleton>
              }
            />
            {Array.from({ length: Math.max(1, rows - 1) }, (_, index) => (
              <InboxCardSkeleton key={index} kind="session" index={index + 1} />
            ))}
          </section>
        </>
      ) : (
        Array.from({ length: rows }, (_, index) => <InboxCardSkeleton key={index} kind="session" index={index} />)
      )}
    </div>
  );
};

/** Rows that take a skeleton's place: they fade in where it was, once it has shown
 *  in this place, and appear at once when they were there from the start. Their
 *  box mounts with the first row, which is what the fade starts from. */
export const SkeletonHandoverRows = ({
  skeleton,
  className,
  children,
}: {
  /** Whether the skeleton these rows replace is showing now. */
  skeleton: boolean;
  className?: string;
  children: ReactNode[];
}) => {
  const handover = useSkeletonHandover(skeleton);
  if (children.length === 0) return null;
  return <div className={clsx(className, handover)}>{children}</div>;
};
