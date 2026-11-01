import type { TranslationKey } from '@/i18n/types';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Layers } from 'lucide-react';
import clsx from 'clsx';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { chatTriggerLink, harnessChipLabelKey } from '../../lib/chatTrigger';
import { formatLocalClockTime } from '../../lib/relativeTime';
import { RoleAvatar } from './RoleAvatar';

// The left rail that ties an expanded group's rows together. Each row draws its
// own segment, reaching up through the column's 12px gap to the row above, so
// the members stay direct children of the transcript column: the scroll anchor
// and the deep-link jump find them exactly as they find any other row.
export const HARNESS_GROUP_RAIL =
  'relative ml-3 pl-4 before:absolute before:-top-3 before:bottom-0 before:left-0 before:w-0.5 before:rounded-full before:bg-cyan/25';

// An Agent callback reads "From · <session>" on its own row, but "From" alone
// says nothing in a count, so the count uses the self-contained "From agent".
const summaryKey = (message: WorkbenchMessage): TranslationKey => {
  const key = harnessChipLabelKey(message);
  return key === 'chat.source.from' ? 'chat.source.harness' : key;
};

const sameLocalDay = (a: string, b: string): boolean => {
  const x = new Date(a);
  const y = new Date(b);
  return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
};

const localDay = (iso: string): string => {
  const d = new Date(iso);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

// First to last member, as wall-clock times; the day appears once the run
// crosses midnight, which overnight Watch runs do.
const timeSpan = (first: string, last: string): string => {
  if (sameLocalDay(first, last)) return `${formatLocalClockTime(first)}–${formatLocalClockTime(last)}`;
  return `${localDay(first)} ${formatLocalClockTime(first)}–${localDay(last)} ${formatLocalClockTime(last)}`;
};

/**
 * The row a run of Harness messages folds into (docs/plans/workbench-refinements.md
 * §3). Collapsed, it is the whole group: a count, where the messages came from,
 * when, and the latest one, stacked over two sheets that say there is more
 * underneath. Expanded, it is only the head; the Transcript draws the members
 * below it on the rail.
 */
export function HarnessGroup({
  members,
  expanded,
  onToggle,
}: {
  members: WorkbenchMessage[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const latest = members[members.length - 1];
  const sources = useMemo(() => {
    const counts = new Map<TranslationKey, number>();
    for (const message of members) {
      const key = summaryKey(message);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts];
  }, [members]);
  const countLabel = t('chat.harnessGroup.count', { count: members.length });
  const link = chatTriggerLink(latest, t('chat.source.agentFallback'));
  const latestSource = link?.kind === 'source' ? link.label : t(harnessChipLabelKey(latest));

  const head = (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 px-0.5 text-left">
      <RoleAvatar tone="cyan">
        <Layers />
      </RoleAvatar>
      <span className="shrink-0 text-[11px] font-semibold text-cyan-ink">{countLabel}</span>
      <span className="inline-flex min-w-0 flex-wrap items-center gap-x-1.5 text-[11px] text-muted">
        {sources.map(([key, count]) => (
          <span key={key} className="whitespace-nowrap">
            {t('chat.harnessGroup.sourceCount', { label: t(key), n: count })}
          </span>
        ))}
      </span>
      <span className="whitespace-nowrap font-mono text-[10px] text-muted">
        {timeSpan(members[0].created_at, latest.created_at)}
      </span>
    </span>
  );

  if (expanded) {
    return (
      <div data-transcript-row className="flex w-full justify-start">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded
          aria-label={countLabel}
          className="group/harness flex w-full max-w-[min(92%,860px)] items-center gap-2 rounded-lg text-left"
        >
          <span className="min-w-0 flex-1">{head}</span>
          <span className="inline-flex shrink-0 items-center gap-0.5 rounded-md bg-cyan/[0.08] px-1.5 py-0.5 text-[11px] font-semibold text-cyan-ink group-hover/harness:bg-cyan/[0.14]">
            {t('chat.collapse')}
            <ChevronDown className="size-3.5 rotate-180" />
          </span>
        </button>
      </div>
    );
  }

  return (
    <div data-transcript-row className="flex w-full justify-start">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={false}
        aria-label={countLabel}
        className="group/harness flex max-w-[min(92%,860px)] flex-col items-start gap-1.5 text-left"
      >
        {head}
        <span className="relative block w-[min(100%,460px)] pb-3">
          {/* Two offset sheets behind the latest message: more is folded in. */}
          <span aria-hidden className="absolute inset-x-5 bottom-0 top-5 rounded-xl border border-cyan/25 bg-background opacity-60" />
          <span aria-hidden className="absolute inset-x-2.5 bottom-1.5 top-2.5 rounded-xl border border-cyan/30 bg-background" />
          <span className="relative block rounded-xl rounded-tl-md bg-background">
            <span
              className={clsx(
                'flex items-center gap-2 rounded-xl rounded-tl-md border border-dashed border-cyan/40 bg-cyan/[0.05] px-3 py-1.5 transition-colors',
                'group-hover/harness:bg-cyan/[0.10]',
              )}
            >
              <span className="shrink-0 rounded bg-cyan/[0.10] px-1 text-[10px] font-bold text-cyan-ink">
                {t('chat.harnessGroup.latest')}
              </span>
              <span className="max-w-[40%] shrink-0 truncate text-[11px] font-medium text-cyan-ink">{latestSource}</span>
              <span className="min-w-0 flex-1 truncate text-[12px] text-muted">{latest.text?.trim()}</span>
              <span className="inline-flex shrink-0 items-center gap-0.5 text-[11px] font-semibold text-cyan-ink">
                {t('chat.harnessGroup.expand')}
                <ChevronDown className="size-3.5" />
              </span>
            </span>
          </span>
        </span>
      </button>
    </div>
  );
}

/** The trailing control of an expanded group, for when its head has scrolled away. */
export function HarnessGroupCollapse({ count, onCollapse }: { count: number; onCollapse: () => void }) {
  const { t } = useTranslation();
  return (
    <div className={HARNESS_GROUP_RAIL}>
      <button
        type="button"
        onClick={onCollapse}
        className="inline-flex items-center gap-0.5 text-[11px] font-semibold text-cyan-ink hover:underline"
      >
        {t('chat.harnessGroup.collapseCount', { count })}
        <ChevronDown className="size-3.5 rotate-180" />
      </button>
    </div>
  );
}
