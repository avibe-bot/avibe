// Proposed components for the Bots sidebar (docs/plans/bots.md D5). Not shipped yet,
// so they live in the design kit; they use only the product's tokens and assets.
import clsx from 'clsx';
import { Inbox, MessageCircle, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import logoImg from '@/assets/logo.png';
import vibeyImg from '@/assets/pet/vibey.png';

export type BotWell = 'mint' | 'cyan' | 'violet' | 'gold' | 'pink';
export type BotAvatar = { kind: 'vibey' } | { kind: 'emoji'; emoji: string; well: BotWell };

export interface BotItem {
  handle: string;
  name: string;
  role: string;
  avatar: BotAvatar;
  unread?: number;
  running?: boolean;
  preview?: string;
  time?: string;
}

const WELL: Record<BotWell, string> = {
  mint: 'bg-mint-soft',
  cyan: 'bg-cyan-soft',
  violet: 'bg-violet-soft',
  gold: 'bg-gold-soft',
  pink: 'bg-pink-soft',
};

export function BotAvatarView({ avatar, size }: { avatar: BotAvatar; size: number }) {
  if (avatar.kind === 'vibey') {
    return (
      <span
        className="grid shrink-0 place-items-center overflow-hidden rounded-full border border-border bg-surface"
        style={{ width: size, height: size }}
      >
        <img src={vibeyImg} alt="" className="size-full scale-[1.12] object-contain" />
      </span>
    );
  }
  return (
    <span
      className={clsx('grid shrink-0 place-items-center rounded-full', WELL[avatar.well])}
      style={{ width: size, height: size, fontSize: size * 0.48 }}
    >
      {avatar.emoji}
    </span>
  );
}

/** The same badge the shipped Inbox row draws. */
function CountBadge({ n, className }: { n: number; className?: string }) {
  return (
    <span
      className={clsx(
        'inline-flex min-w-[1.25rem] items-center justify-center rounded-full bg-cyan px-1.5 py-0.5 font-mono text-[9px] font-bold text-accent-foreground shadow-glow-xs-cyan',
        className,
      )}
    >
      {n > 99 ? '99+' : n}
    </span>
  );
}

/** Brand row with the Inbox as an icon at its right end; the subtitle drops "Workbench". */
export function BrandRowWithInbox({ inboxUnread = 0 }: { inboxUnread?: number }) {
  const { t } = useTranslation();
  return (
    <div className="flex shrink-0 items-center pb-3">
      <div className="flex min-w-0 items-center gap-2.5">
        <img
          src={logoImg}
          alt=""
          className="size-8 shrink-0 rounded-lg border border-mint/35 bg-mint/[0.08] object-cover shadow-glow-sm-mint"
        />
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[13px] font-semibold text-foreground">{t('appShell.title')}</div>
          <div className="truncate text-[11px] text-muted">Agent OS</div>
        </div>
      </div>
      <span
        aria-label={t('workbench.nav.inbox')}
        className="relative ml-auto grid size-8 shrink-0 place-items-center rounded-lg text-foreground hover:bg-foreground/[0.04]"
      >
        <Inbox className={clsx('size-[18px]', inboxUnread > 0 && 'text-cyan-ink')} />
        {inboxUnread > 0 && <CountBadge n={inboxUnread} className="absolute -right-1.5 -top-1" />}
      </span>
    </div>
  );
}

function Slot({ bot, selected }: { bot: BotItem; selected?: boolean }) {
  return (
    <div className="flex w-[50px] shrink-0 flex-col items-center gap-1">
      <div className={clsx('relative rounded-full p-[2px]', selected && 'ring-2 ring-mint')}>
        <BotAvatarView avatar={bot.avatar} size={40} />
        {bot.unread ? <CountBadge n={bot.unread} className="absolute -right-2 -top-1" /> : null}
        {bot.running && (
          <span className="absolute -bottom-0.5 -right-0.5 grid size-3.5 place-items-center rounded-full bg-[var(--sidebar-background)]">
            <span className="size-2 animate-pulse rounded-full bg-mint shadow-glow-dot-mint" />
          </span>
        )}
      </div>
      <span className={clsx('w-full truncate text-center text-[11px] leading-tight', selected ? 'font-semibold text-foreground' : 'text-muted')}>
        {bot.name}
      </span>
    </div>
  );
}

/** The Bot strip that takes the Inbox row's place. */
export function BotStrip({ bots, selected = 'main' }: { bots: BotItem[]; selected?: string }) {
  return (
    <div className="flex shrink-0 flex-col gap-1.5">
      <span className="px-1 font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">伙伴</span>
      <div className="flex items-start gap-1 px-0.5 pt-1">
        {bots.map((bot) => (
          <Slot key={bot.handle} bot={bot} selected={bot.handle === selected} />
        ))}
        <div className="flex w-[50px] shrink-0 flex-col items-center gap-1">
          <div className="p-[2px]">
            <span className="grid size-10 place-items-center rounded-full border border-dashed border-border-strong text-muted">
              <Plus className="size-4" />
            </span>
          </div>
          <span className="text-[11px] leading-tight text-muted">添加</span>
        </div>
      </div>
    </div>
  );
}

/** Hover card: what the strip cannot show inline. */
export function BotHoverCard({ bot }: { bot: BotItem }) {
  return (
    <div className="flex w-[248px] flex-col gap-3 rounded-2xl border border-border-strong bg-surface-2 p-3.5 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.35)]">
      <div className="flex items-center gap-3">
        <BotAvatarView avatar={bot.avatar} size={44} />
        <div className="min-w-0 flex-1 leading-tight">
          <div className="truncate text-[14px] font-bold text-foreground">{bot.name}</div>
          <div className="mt-0.5 font-mono text-[11px] text-muted">@{bot.handle}</div>
        </div>
      </div>
      <div className="text-[12px] leading-relaxed text-muted">{bot.role}</div>
      {bot.preview && (
        <div className="rounded-lg bg-foreground/[0.04] px-2.5 py-2 text-[12px] leading-relaxed text-foreground">
          <div className={clsx('mb-0.5 text-[10px] font-semibold', bot.running ? 'text-mint-ink' : 'text-muted')}>
            {bot.running ? '运行中' : '最近'} · {bot.time}
          </div>
          {bot.preview}
        </div>
      )}
      <div className="flex items-center gap-1.5 text-[12px] font-semibold text-cyan-ink">
        <MessageCircle className="size-3.5" />
        进入对话
      </div>
    </div>
  );
}
