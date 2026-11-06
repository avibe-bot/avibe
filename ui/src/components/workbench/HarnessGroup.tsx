import { useTranslation } from 'react-i18next';
import { ChevronDown, Layers } from 'lucide-react';
import clsx from 'clsx';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { chatTriggerLink, harnessChipLabelKey } from '../../lib/chatTrigger';
import { ITEM_KEY_ATTRIBUTE, REVEAL_ATTRIBUTE } from '../../lib/transcriptMotion';
import { RoleAvatar } from './RoleAvatar';

// The left rail that ties an expanded group's rows together. Each row draws its
// own segment, reaching up through the column's 12px gap to the row above, so
// the members stay direct children of the transcript column: the scroll anchor
// and the deep-link jump find them exactly as they find any other row.
export const HARNESS_GROUP_RAIL =
  'relative ml-3 pl-4 before:absolute before:-top-3 before:bottom-0 before:left-0 before:w-0.5 before:rounded-full before:bg-cyan/25';

/**
 * The row a run of Harness messages folds into (docs/plans/workbench-refinements.md
 * §3). Collapsed, it is the whole group: a count and the latest message,
 * stacked over two sheets that say there is more underneath. Expanded, it is
 * only the head; the Transcript draws the members below it on the rail.
 */
export function HarnessGroup({
  groupKey,
  members,
  expanded,
  closing = false,
  onToggle,
}: {
  /** Stable across growth; the Transcript finds the head by it after a collapse. */
  groupKey: string;
  members: WorkbenchMessage[];
  expanded: boolean;
  /** Closed, while its members leave in place: still drawn as open, so nothing moves until they are gone. */
  closing?: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const latest = members[members.length - 1];
  const countLabel = t('chat.harnessGroup.count', { count: members.length });
  const link = chatTriggerLink(latest, t('chat.source.agentFallback'));
  const latestSource = link?.kind === 'source' ? link.label : t(harnessChipLabelKey(latest));

  const head = (
    <span className="flex min-w-0 items-center gap-2 px-0.5 text-left">
      <RoleAvatar tone="cyan">
        <Layers />
      </RoleAvatar>
      <span className="shrink-0 text-[11px] font-semibold text-cyan-ink">{countLabel}</span>
    </span>
  );

  // The head's identity for the transcript's scroll anchor: which group it is,
  // and the messages it stands for, so an anchor held on one of them can find
  // the head they collapsed into and the head can find them if it dissolves.
  const rowAttributes = {
    'data-transcript-row': '',
    'data-harness-group': groupKey,
    'data-harness-members': members.map((message) => message.id).join(' '),
  };

  if (expanded || closing) {
    return (
      <div {...rowAttributes} className="flex w-full justify-start">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!closing}
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
    <div {...rowAttributes} className="flex w-full justify-start">
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

/** The trailing control of an expanded group, for when its head has scrolled away. A transcript item of
 *  its own (lib/transcriptMotion), which the scroller can hold. */
export function HarnessGroupCollapse({ groupKey, count, onCollapse, closing = false }: { groupKey: string; count: number; onCollapse: () => void; closing?: boolean }) {
  const { t } = useTranslation();
  return (
    <div
      data-transcript-row=""
      {...{ [ITEM_KEY_ATTRIBUTE]: `harness-collapse:${groupKey}`, [REVEAL_ATTRIBUTE]: '' }}
      aria-hidden={closing || undefined}
      className={clsx(HARNESS_GROUP_RAIL, closing && 'motion-disclose-out-children')}
    >
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
