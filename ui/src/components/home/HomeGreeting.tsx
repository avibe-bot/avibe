import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Link, useNavigate } from 'react-router-dom';

import { BotAvatar } from '@/components/ui/bot-avatar';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { BotFace } from '@/components/workbench/bots/BotFace';
import { BotSwitchRow } from '@/components/workbench/bots/BotSwitch';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { botSessionPath, type Bot } from '@/lib/bots';
import { useIsDesktop } from '@/lib/useIsDesktop';
import { timeOfDay } from './homeCopy';

/**
 * The group's first block (ui/design: home/home/*): the main Bot's face, the greeting, and, below
 * it, what is happening, only when something is. Before the main Bot has a model it introduces
 * itself, since it cannot answer yet; after that it asks what to do. On a phone the face is left
 * out (the tab bar's center is the main Bot), and so is the unread count (the Inbox tab has it).
 * A member has no main Bot: no face, and the greeting asks.
 */
export function HomeGreeting({ bot, name, introduce }: { bot: Bot | null; name: string; introduce: boolean }) {
  const { t } = useTranslation();
  const desktop = useIsDesktop();
  const part = timeOfDay(new Date());
  return (
    <div className={clsx('flex flex-col items-center text-center', desktop ? 'gap-3' : 'gap-2')}>
      {bot && desktop && <BotAvatar avatar={bot.avatar} name={name} size={64} />}
      <h1 className={clsx('font-bold text-foreground', desktop ? 'text-3xl' : 'text-2xl')}>
        {introduce
          ? t(`workbench.home.greeting.${part}Intro`, { name })
          : t(`workbench.home.greeting.${part}`)}
      </h1>
      <HomeStatus desktop={desktop} />
    </div>
  );
}

/** The Bots at work (their faces) and the unread replies, beside the greeting. Each is a way in:
 *  one working Bot opens its conversation, several list themselves to pick from, and the unread
 *  count opens the Inbox. */
function HomeStatus({ desktop }: { desktop: boolean }) {
  const { t } = useTranslation();
  const { bots, statusBySession } = useBots();
  const { totalUnread } = useWorkbenchInbox({ feed: false });
  const running = bots.filter((bot) => statusBySession[bot.session_id] === 'running');
  const unread = desktop ? totalUnread : 0;
  if (running.length === 0 && unread === 0) return null;
  return (
    <div className="flex shrink-0 items-center gap-2 text-xs">
      {running.length > 0 && <BusyBots running={running} />}
      {unread > 0 && (
        <Link
          to="/inbox"
          className="inline-flex h-7 items-center gap-1.5 rounded-full border border-cyan/30 bg-cyan/5 px-2.5 transition hover:bg-cyan/10"
        >
          <span className="size-1.5 rounded-full bg-cyan-ink" />
          <span className="text-foreground">{t('workbench.home.status.unread', { count: unread })}</span>
        </Link>
      )}
    </div>
  );
}

const BUSY_CHIP_CLASS = 'inline-flex h-7 items-center gap-1.5 rounded-full border border-mint/30 bg-mint/5 pl-1 pr-2.5 transition hover:bg-mint-soft';

/** "N working": one Bot goes straight to its conversation; several open a short list of them (a
 *  popover on a desktop, a sheet on a phone), and the one picked opens. */
function BusyBots({ running }: { running: Bot[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { displayName } = useBots();
  const [open, setOpen] = useState(false);
  const label = t('workbench.home.status.working', { count: running.length });
  const chip = (
    <>
      <span className="flex -space-x-1.5">
        {running.map((bot) => (
          <BotFace key={bot.handle} avatar={bot.avatar} name={displayName(bot)} size={20} dotWell="bg-surface" />
        ))}
      </span>
      <span className="text-mint-ink">{label}</span>
    </>
  );
  if (running.length === 1) {
    return <Link to={botSessionPath(running[0])} className={BUSY_CHIP_CLASS}>{chip}</Link>;
  }
  return (
    <ResponsiveMenu
      open={open}
      onOpenChange={setOpen}
      trigger={<button type="button" aria-haspopup="dialog" className={BUSY_CHIP_CLASS}>{chip}</button>}
      sheetTitle={label}
      align="center"
      className="w-80"
    >
      {open && <BusyList running={running} onPick={(bot) => { setOpen(false); navigate(botSessionPath(bot)); }} />}
    </ResponsiveMenu>
  );
}

/** Mounted only while the list is open, so only then does it follow the Inbox feed for the previews. */
function BusyList({ running, onPick }: { running: Bot[]; onPick: (bot: Bot) => void }) {
  useWorkbenchInbox();
  return (
    <ul className="flex flex-col gap-1 p-1">
      {running.map((bot) => (
        <li key={bot.handle}>
          <BotSwitchRow bot={bot} onPick={() => onPick(bot)} />
        </li>
      ))}
    </ul>
  );
}
