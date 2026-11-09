import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';
import { BellRing, ChevronDown, ChevronRight, Cpu, MessagesSquare, MousePointer2, Rocket, Smartphone, X } from 'lucide-react';
import clsx from 'clsx';

import type { SetupItem } from '@/lib/homePreferences';
import { useIsDesktop } from '@/lib/useIsDesktop';
import { Button } from '../ui/button';

const ICONS: Record<SetupItem, LucideIcon> = {
  model: Cpu,
  notifications: BellRing,
  computer_use: MousePointer2,
  chat_apps: MessagesSquare,
  phone: Smartphone,
};

/**
 * What is still missing (ui/design: home/home/a-new-*, b-partial-*). While there is no model only
 * that item shows, and it leads: the one accented row on the page, with a short line on what it
 * takes, and the page's one solid button. Otherwise the first two show (one on a phone), quiet,
 * one line each, with what each is for as their hover text; the rest fold into "N more". "Later"
 * hides the card until tomorrow; × on an item ("Not needed") removes it for good.
 */
export function GettingStarted({
  pending,
  name,
  onAction,
  onNotNeeded,
  onLater,
}: {
  pending: SetupItem[];
  /** Who the copy speaks of: the main Bot's name. */
  name: string;
  onAction: (item: SetupItem) => void;
  onNotNeeded: (item: SetupItem) => void;
  onLater: () => void;
}) {
  const { t } = useTranslation();
  const desktop = useIsDesktop();
  const [expanded, setExpanded] = useState(false);
  if (pending.length === 0) return null;
  const shown = pending.includes('model')
    ? (['model'] as SetupItem[])
    : expanded ? pending : pending.slice(0, desktop ? 2 : 1);
  const rest = pending.length - shown.length;
  const copy = (item: SetupItem, part: 'title' | 'why' | 'action') =>
    t(`workbench.home.setup.${item}.${part}`, { name });
  return (
    <section className="flex w-full flex-col rounded-2xl border border-edge bg-surface p-1.5 shadow-elevation-0">
      <header className={clsx('flex items-center gap-2 px-2.5', desktop ? 'h-8' : 'h-7')}>
        <Rocket className="size-4 text-mint-ink" />
        <h2 className="text-sm font-semibold text-foreground">{t('workbench.home.setup.title')}</h2>
        <span className="flex-1" />
        {rest > 0 && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="inline-flex items-center gap-0.5 text-xs text-muted hover:text-foreground"
          >
            {t('workbench.home.setup.more', { count: rest })}
            <ChevronDown className="size-3.5" />
          </button>
        )}
        <button type="button" onClick={onLater} className="px-1 text-xs text-muted hover:text-foreground">
          {t('workbench.home.setup.later')}
        </button>
      </header>
      {shown.map((item) => {
        const Icon = ICONS[item];
        const leads = item === 'model';
        return (
          <div
            key={item}
            title={leads ? undefined : copy(item, 'why')}
            className={clsx('flex items-center gap-3 rounded-xl px-2.5', leads ? 'bg-gold-soft py-3' : desktop ? 'h-12' : 'h-11')}
          >
            <span className={clsx('grid shrink-0 place-items-center rounded-lg', leads ? 'size-10 bg-surface text-gold-ink' : 'size-8 bg-surface-2 text-muted')}>
              <Icon className={leads ? 'size-5' : 'size-4'} />
            </span>
            {leads ? (
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="truncate text-base font-semibold text-foreground">{copy(item, 'title')}</span>
                <span className={clsx('text-xs text-muted', desktop && 'truncate')}>{copy(item, 'why')}</span>
              </span>
            ) : (
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">{copy(item, 'title')}</span>
            )}
            {leads ? (
              <Button type="button" variant="brand" size="sm" className="shrink-0 gap-1" onClick={() => onAction(item)}>
                {copy(item, 'action')}
                <ChevronRight className="size-3.5" />
              </Button>
            ) : (
              <>
                <Button type="button" variant="text" size="compact" onClick={() => onAction(item)}>
                  {copy(item, 'action')}
                  <ChevronRight />
                </Button>
                <button
                  type="button"
                  onClick={() => onNotNeeded(item)}
                  aria-label={t('workbench.home.setup.notNeeded')}
                  title={t('workbench.home.setup.notNeeded')}
                  className="-mr-1 grid size-7 shrink-0 place-items-center rounded-md text-muted hover:bg-foreground/5 hover:text-foreground"
                >
                  <X className="size-3.5" />
                </button>
              </>
            )}
          </div>
        );
      })}
    </section>
  );
}
