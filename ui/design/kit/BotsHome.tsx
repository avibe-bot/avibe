// Variant B: a Bots tab. Its page leads with the main Bot, lists the others with
// what each last said, unread, and running, and docks a one-line composer to the
// main Bot above the tab bar, so the main Bot is one thumb away on this tab.
import { FolderTree, Grid2x2, Inbox, Plus, UserPlus, UsersRound } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { MobileTabBar } from '@/components/MobileChrome';
import { BotAvatar } from '@/components/ui/bot-avatar';
import { Button } from '@/components/ui/button';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import type { Bot } from '@/lib/bots';
import { formatRelativeTime } from '@/lib/relativeTime';

import { BotFace } from './BotFace';
import { useDraft } from './draft';
import { MainBotQuickComposer } from './MainBotQuickComposer';

/** The Bots tab's icon: the main Bot's own avatar, so the tab reads as "your Bots". */
export function BotsTabIcon() {
  const { mainBot, displayName } = useBots();
  if (!mainBot) return <UsersRound className="size-4" />;
  return <BotAvatar avatar={mainBot.avatar} name={displayName(mainBot)} size={22} />;
}

/** The bar with the Bots tab first: Bots · Inbox · ＋ · Projects · Apps (Capabilities moves into the
 *  Apps drawer). The shipped bar, with these items. */
export function BotsTabBar({ inboxBadge }: { inboxBadge: number }) {
  const { t } = useTranslation();
  const draft = useDraft();
  return (
    <MobileTabBar
      items={[
        { to: '/bots', label: draft('伙伴', 'Bots'), icon: BotsTabIcon },
        { to: '/inbox', label: t('nav.inbox'), icon: Inbox, badge: inboxBadge },
        { to: '/projects', label: t('nav.projects'), icon: FolderTree },
        { label: t('nav.apps'), icon: Grid2x2, onClick: () => {} },
      ]}
      center={{ onClick: () => {}, label: t('appShell.newSession'), icon: Plus }}
    />
  );
}

function MainBotCard({ bot }: { bot: Bot }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { displayName, statusBySession } = useBots();
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  const name = displayName(bot);
  const row = inboxSessions.find((s) => s.session_id === bot.session_id);
  const running = statusBySession[bot.session_id] === 'running';
  const unread = unreadBySession[bot.session_id] ?? 0;
  return (
    <button
      type="button"
      className={clsx(
        'flex flex-col gap-3 rounded-2xl border p-4 text-left transition',
        unread > 0 || running ? 'border-mint/35 bg-mint/[0.05] shadow-glow-md-mint' : 'border-border bg-surface',
      )}
    >
      <span className="flex items-center gap-3">
        <BotFace bot={bot} name={name} size={52} unread={unread} running={running} />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex items-center gap-1.5">
            <span className="truncate text-[17px] font-bold text-foreground">{name}</span>
            <span className="shrink-0 rounded bg-mint-soft px-1 text-[9px] font-semibold leading-[14px] text-mint-ink">{draft('主伙伴', 'Main')}</span>
          </span>
          <span className="truncate text-[12px] text-muted">{draft('你的第一联系人', 'Your first point of contact')}</span>
        </span>
        {row && <span className="shrink-0 self-start font-mono text-[10.5px] text-muted">{formatRelativeTime(row.last_activity_at, t)}</span>}
      </span>
      {running ? (
        <span className="flex items-center gap-2 rounded-xl bg-surface px-3 py-2 text-[12.5px]">
          <span className="relative size-2 shrink-0">
            <span className="absolute inset-0 animate-ping rounded-full bg-mint opacity-70 motion-reduce:animate-none" />
            <span className="relative block size-2 rounded-full bg-mint shadow-glow-dot-mint" />
          </span>
          <span className="shrink-0 font-semibold text-mint-ink">{t('bots.running')}</span>
          <span className="min-w-0 truncate text-muted">{row?.preview_text}</span>
        </span>
      ) : (
        row && <span className="line-clamp-2 text-[14px] leading-relaxed text-foreground">{row.preview_text}</span>
      )}
    </button>
  );
}

export function BotsHome() {
  const { t } = useTranslation();
  const draft = useDraft();
  const { bots, mainBot, displayName, statusBySession } = useBots();
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  const others = bots.filter((bot) => !bot.builtin);
  const preview = (bot: Bot) => inboxSessions.find((s) => s.session_id === bot.session_id);
  const othersUnread = others.reduce((sum, bot) => sum + (unreadBySession[bot.session_id] ?? 0), 0);


  return (
    <div className="mx-auto flex max-w-xl flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">{draft('伙伴', 'Bots')}</h1>
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label={draft('添加伙伴', 'Add a Bot')}
          className="border-mint/35 bg-mint/[0.08] text-mint-ink hover:bg-mint/[0.14]"
        >
          <UserPlus className="size-4" />
        </Button>
      </div>
      {mainBot && <MainBotCard bot={mainBot} />}
      {others.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h2 className="flex items-center gap-2 px-1 text-[12px] font-semibold text-muted">
            {draft('其他伙伴', 'Other Bots')}
            {othersUnread > 0 && (
              <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-cyan px-1 font-mono text-[9px] font-bold text-accent-foreground">
                {othersUnread}
              </span>
            )}
          </h2>
          {others.map((bot) => {
            const name = displayName(bot);
            const row = preview(bot);
            const running = statusBySession[bot.session_id] === 'running';
            const unread = unreadBySession[bot.session_id] ?? 0;
            return (
              <button
                key={bot.handle}
                type="button"
                className={clsx(
                  'flex items-center gap-3 rounded-xl border px-3 py-3 text-left',
                  unread > 0 ? 'border-cyan/30 bg-cyan/[0.04]' : 'border-border bg-surface',
                )}
              >
                <BotFace bot={bot} name={name} size={44} unread={unread} running={running} />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="truncate text-[15px] font-semibold text-foreground">{name}</span>
                  <span className={clsx('truncate text-[12px]', unread > 0 ? 'text-foreground' : 'text-muted')}>
                    {running && <span className="font-semibold text-mint-ink">{t('bots.running')} · </span>}
                    {row?.preview_text || bot.role}
                  </span>
                </span>
                {row && <span className="shrink-0 self-start font-mono text-[10.5px] text-muted">{formatRelativeTime(row.last_activity_at, t)}</span>}
              </button>
            );
          })}
        </section>
      ) : (
        <section className="flex flex-col items-center gap-2 rounded-2xl border border-dashed border-border-strong px-5 py-6 text-center">
          <span className="grid size-10 place-items-center rounded-full bg-mint-soft text-mint-ink">
            <UsersRound className="size-5" />
          </span>
          <span className="text-[15px] font-semibold text-foreground">{draft('添加一位伙伴', 'Add a Bot')}</span>
          <span className="text-[12.5px] leading-relaxed text-muted">
            {draft('伙伴有名字、职责和自己的记忆，和它的对话会一直延续。', 'A Bot has a name, a role, and its own memory, and your conversation with it carries on.')}
          </span>
          <Button type="button" variant="outline" size="sm" className="mt-1 gap-1.5 rounded-full border-mint/35 bg-mint/[0.08] text-mint-ink">
            <UserPlus className="size-3.5" />
            {draft('添加伙伴', 'Add a Bot')}
          </Button>
          {mainBot && (
            <span className="mt-1 text-[11.5px] text-muted">
              {draft(`也可以直接跟${displayName(mainBot)}说「帮我加一个负责安全的伙伴」`, `Or tell ${displayName(mainBot)}: “Add a Bot that handles security.”`)}
            </span>
          )}
        </section>
      )}
      {/* Clear of the docked composer. */}
      <div className="h-16" />
      <MainBotQuickComposer docked />
    </div>
  );
}
