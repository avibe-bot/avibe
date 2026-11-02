// Home (owner direction, 2026-11-02 15:48): a home page that talks to the main Bot
// with no project to pick, guides a new user through what is still missing, and
// keeps teaching what Avibe can do, one tip at a time. Once set up it stays calm:
// the composer, a line of what is happening, the suggestions, and the tip. A
// member without Bots gets the same page, with a composer that starts a Session
// in a project as today. The composer and the project chips are the shipped ones.
import type { LucideIcon } from 'lucide-react';
import {
  Activity,
  AppWindow,
  BellRing,
  BookmarkPlus,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Cpu,
  FolderKanban,
  KeyRound,
  Lightbulb,
  MessagesSquare,
  Mic,
  MonitorSmartphone,
  MousePointer2,
  Puzzle,
  Rocket,
  Smartphone,
  UserPlus,
  Users,
  X,
} from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { BotAvatar } from '@/components/ui/bot-avatar';
import { Button } from '@/components/ui/button';
import { Composer } from '@/components/workbench/Composer';
import { ProjectPicker } from '@/components/workbench/ProjectPicker';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { useWorkbenchProjectsTree } from '@/context/WorkbenchProjectsContext';

import { BotFace } from './BotFace';
import { useDraft } from './draft';

/** A setup gap the UI can already observe, and where it is fixed:
 *  model — the main Bot's backend reports not ready (backend readiness) → Settings · Models;
 *  notifications — Web Push off on this device, or the desktop app's notification permission → turn on here;
 *  computerUse — the desktop app's Computer Use status (accessibility, screen recording) → its guide;
 *  chatApps — no IM platform enabled in the config → Settings · Platforms;
 *  phone — Avibe Cloud remote access not paired → Settings · Remote access. */
export type SetupGap = 'model' | 'notifications' | 'computerUse' | 'chatApps' | 'phone';

function useGapCopy() {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  // A member has no main Bot: the copy speaks of the Agent that runs the work.
  const name = mainBot ? displayName(mainBot) : 'Agent';
  const rows: Record<SetupGap, { icon: LucideIcon; title: string; why: string; action: string; blocking?: boolean }> = {
    model: {
      icon: Cpu,
      title: draft(`给${name}接上模型`, `Give ${name} a model`),
      why: draft(`${name}还没有能用的模型。加一个已有的订阅或 API key 就行。`, `${name} has no model yet. Add a subscription or API key you already have.`),
      action: draft('去添加', 'Add one'),
      blocking: true,
    },
    notifications: {
      icon: BellRing,
      title: draft('打开通知', 'Turn on notifications'),
      why: draft(`${name} 做完事、需要你决定的时候，第一时间告诉你。`, `Hear from ${name} as soon as work is done or needs you.`),
      action: draft('打开', 'Turn on'),
    },
    computerUse: {
      icon: MousePointer2,
      title: draft('允许操作电脑', 'Allow Computer Use'),
      why: draft('需要辅助功能和屏幕录制权限，之后可以替你操作这台电脑上的应用。', 'Grant Accessibility and Screen Recording so apps on this computer can be used for you.'),
      action: draft('去授权', 'Grant'),
    },
    chatApps: {
      icon: MessagesSquare,
      title: draft('连接聊天应用', 'Connect a chat app'),
      why: draft(`在 Slack、飞书、微信里也能找${name}。`, `Reach ${name} from Slack, Lark, or WeChat too.`),
      action: draft('去连接', 'Connect'),
    },
    phone: {
      icon: Smartphone,
      title: draft('在手机上继续', 'Continue on your phone'),
      why: draft('配对 Avibe Cloud：手机上能用、能收通知、能语音。', 'Pair Avibe Cloud to use it, get notified, and speak on your phone.'),
      action: draft('去配对', 'Pair'),
    },
  };
  return rows;
}

/** Shown until nothing is missing; each row says what is missing and goes straight to its fix.
 *  Done rows disappear; "later" folds the card into one line for this visit. */
export function GettingStarted({ pending, compact = false }: { pending: SetupGap[]; compact?: boolean }) {
  const draft = useDraft();
  const rows = useGapCopy();
  if (pending.length === 0) return null;
  return (
    <section className="flex w-full flex-col gap-1 rounded-2xl border border-border bg-surface p-2 shadow-mint-card-sm">
      <header className="flex items-center gap-2 px-2 pb-1 pt-1.5">
        <Rocket className="size-4 text-mint-ink" />
        <h2 className="text-sm font-semibold text-foreground">{draft('开始使用 Avibe', 'Get started')}</h2>
        <span className="rounded-full bg-mint-soft px-2 py-0.5 text-2xs font-semibold text-mint-ink">
          {draft(`还有 ${pending.length} 步`, `${pending.length} left`)}
        </span>
        <span className="flex-1" />
        <button type="button" className="text-xs text-muted hover:text-foreground">{draft('稍后', 'Later')}</button>
      </header>
      {pending.map((gap) => {
        const { icon: Icon, title, why, action, blocking } = rows[gap];
        return (
          <div key={gap} className={clsx('flex items-center gap-3 rounded-xl px-2 py-2.5', blocking && 'bg-gold-soft')}>
            <span className={clsx('grid size-8 shrink-0 place-items-center rounded-lg', blocking ? 'bg-surface text-gold-ink' : 'bg-surface-2 text-muted')}>
              <Icon className="size-4" />
            </span>
            <span className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="text-sm font-semibold text-foreground">{title}</span>
              <span className={clsx('text-xs text-muted', compact ? 'line-clamp-2' : 'truncate')}>{why}</span>
            </span>
            <Button type="button" variant={blocking ? 'brand' : 'outline'} size="xs" className="shrink-0 gap-1">
              {action}
              <ChevronRight className="size-3.5" />
            </Button>
          </div>
        );
      })}
    </section>
  );
}

/** A short line of what is happening: Bots at work and replies waiting, each a way in. Nothing → nothing. */
export function HomeStatusLine() {
  const { t } = useTranslation();
  const draft = useDraft();
  const { bots, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const running = bots.filter((bot) => statusBySession[bot.session_id] === 'running');
  const unread = Object.values(unreadBySession).reduce((sum, n) => sum + n, 0);
  if (running.length === 0 && unread === 0) return null;
  return (
    <div className="flex flex-wrap items-center justify-center gap-2 text-xs">
      {running.map((bot) => (
        <span key={bot.handle} className="inline-flex items-center gap-1.5 rounded-full border border-mint/30 bg-mint/5 py-1 pl-1 pr-2.5">
          <BotFace bot={bot} name={displayName(bot)} size={20} running dotWell="bg-surface" />
          <span className="font-semibold text-foreground">{displayName(bot)}</span>
          <span className="text-mint-ink">{t('bots.running')}</span>
        </span>
      ))}
      {unread > 0 && (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-cyan/30 bg-cyan/5 px-2.5 py-1">
          <CircleDot className="size-3.5 text-cyan-ink" />
          <span className="text-foreground">{draft(`${unread} 条未读回复`, `${unread} unread replies`)}</span>
        </span>
      )}
    </div>
  );
}

/** The composer's route: the main Bot, its model. A member's shows the Agent the Session will run. */
function RouteLabel({ member }: { member: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  return (
    <span className="flex items-center gap-1.5 text-xs">
      {!member && mainBot && <BotAvatar avatar={mainBot.avatar} name={displayName(mainBot)} size={18} />}
      <span className="font-semibold text-foreground">{member || !mainBot ? 'vibey' : displayName(mainBot)}</span>
      <span className="font-mono text-muted">· gpt-6-astra · {draft('高', 'high')}</span>
    </span>
  );
}

function useAsks(member: boolean) {
  const draft = useDraft();
  const asks: { icon: LucideIcon; label: string }[] = [
    ...(member ? [] : [{ icon: UserPlus, label: draft('加一个负责安全的伙伴', 'Add a Bot for security') }]),
    { icon: FolderKanban, label: draft('各个项目进展怎么样？', 'How are the projects going?') },
    { icon: Rocket, label: draft('在 avibe 里把缓存问题修掉', 'Fix the cache bug in avibe') },
    { icon: BookmarkPlus, label: draft('记住：周一上午不排会', 'Remember: no meetings Monday mornings') },
    { icon: Activity, label: draft('每周一早上发我一份周报', 'Send me a report every Monday morning') },
  ];
  return asks;
}

type Tip = { icon: LucideIcon; title: string; why: string; ask?: string; link?: string };

function useTips(): Tip[] {
  const draft = useDraft();
  return [
    { icon: Users, title: draft('伙伴', 'Bots'), why: draft('给长期的事配一个有名字、有记忆的伙伴，它会一直接着做。', 'Give an ongoing job a Bot with a name and a memory; it keeps at it.'), ask: draft('帮我加一个负责发布的伙伴', 'Add a Bot that owns releases') },
    { icon: AppWindow, title: draft('可视化页面', 'Show Pages'), why: draft('让对话把结果画成一个页面：图表、对比、原型，手机上也能看。', 'Have a conversation draw its result as a page: charts, comparisons, prototypes.'), ask: draft('把本周的发布进展画成一个页面', "Make a page of this week's release progress") },
    { icon: Activity, title: draft('定时任务和监听', 'Tasks and Watches'), why: draft('到点自动做，或者等某件事发生再做，比如 CI 失败。', 'Run on a schedule, or wait for something to happen, like a failing CI.'), ask: draft('PR 的 CI 失败时告诉我原因', "Tell me why when a PR's CI fails") },
    { icon: KeyRound, title: draft('保险库', 'Vault'), why: draft('密钥存在保险库里，Agent 用得到，但永远看不到原文。', 'Keys live in the Vault: Agents use them without ever seeing them.'), link: draft('打开保险库', 'Open the Vault') },
    { icon: Puzzle, title: draft('技能和 MCP', 'Skills & MCP'), why: draft('装一个技能或 MCP 服务器，所有 Agent 就多一项本事。', 'Install a Skill or an MCP server and every Agent can use it.'), link: draft('看看技能', 'Browse Skills') },
    { icon: MessagesSquare, title: draft('聊天应用', 'Chat apps'), why: draft('在 Slack、飞书、微信里直接和歪比说话，回复也在那里。', 'Talk to Vibey from Slack, Lark, or WeChat, and get replies there.'), link: draft('连接聊天应用', 'Connect one') },
    { icon: MonitorSmartphone, title: draft('桌面小窗', 'The desktop pet'), why: draft('桌面应用里歪比常驻在屏幕角落，按住就能说话。', 'In the desktop app Vibey sits in a corner of your screen; hold it to talk.'), link: draft('下载桌面应用', 'Get the desktop app') },
    { icon: Mic, title: draft('语音', 'Voice'), why: draft('手机上按住底栏中间的歪比说话，松开就发给它。', 'On a phone, hold Vibey in the middle of the tab bar to talk; release to send.'), link: draft('了解语音', 'About voice') },
  ];
}

/** One capability at a time. The build picks the day's tip from those the user has not used yet;
 *  ‹ › shows the others, × hides the card until tomorrow. */
export function TipOfTheDay({ index, member = false }: { index: number; member?: boolean }) {
  const draft = useDraft();
  // A member has no Bots, so the Bots tip is not theirs to try.
  const tips = useTips().filter((tip) => !member || tip.icon !== Users);
  const tip = tips[index % tips.length];
  const Icon = tip.icon;
  return (
    <section className="flex w-full items-start gap-3 rounded-2xl border border-border bg-surface-2 p-4">
      <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-cyan-soft text-cyan-ink">
        <Icon className="size-4" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex items-center gap-2 text-2xs font-semibold uppercase tracking-wider text-muted">
          <Lightbulb className="size-3.5 text-gold-ink" />
          {draft('今天的小技巧', 'Tip of the day')}
          <span className="font-mono normal-case tracking-normal">{index % tips.length + 1}/{tips.length}</span>
          <span className="flex-1" />
          <span className="-my-1.5 flex shrink-0 items-center gap-0.5 normal-case tracking-normal">
            <button type="button" aria-label={draft('上一条', 'Previous')} className="grid size-7 place-items-center rounded-md hover:bg-foreground/5"><ChevronLeft className="size-4" /></button>
            <button type="button" aria-label={draft('下一条', 'Next')} className="grid size-7 place-items-center rounded-md hover:bg-foreground/5"><ChevronRight className="size-4" /></button>
            <button type="button" aria-label={draft('今天不再显示', 'Hide for today')} className="grid size-7 place-items-center rounded-md hover:bg-foreground/5"><X className="size-4" /></button>
          </span>
        </div>
        <span className="text-sm font-semibold text-foreground">{tip.title}</span>
        <span className="text-xs leading-relaxed text-muted">{tip.why}</span>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          {tip.ask && (
            <button type="button" className="rounded-full border border-mint/35 bg-mint/8 px-3 py-1 text-xs text-mint-ink">
              {draft('试试：', 'Try: ')}{tip.ask}
            </button>
          )}
          {tip.link && (
            <button type="button" className="inline-flex items-center gap-1 text-xs font-semibold text-cyan-ink">
              {tip.link}
              <ChevronRight className="size-3.5" />
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

/** The whole home. `pending` is the setup still missing; `tip` which tip is up; `member` a reader without Bots.
 *  The tip waits while a blocking step (no model) is pending: until the main Bot can work, teaching
 *  what else it can do is noise. */
export function HomePage({ pending, tip, member = false, compact = false }: { pending: SetupGap[]; tip: number; member?: boolean; compact?: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  const { projects } = useWorkbenchProjectsTree();
  const asks = useAsks(member);
  const name = mainBot ? displayName(mainBot) : '';
  return (
    <div className={clsx('mx-auto flex w-full max-w-2xl flex-col items-center', compact ? 'gap-4' : 'gap-5 pt-6')}>
      <div className="flex flex-col items-center gap-2 text-center">
        {/* On a phone the bar's center is the main Bot already; the heading carries it small. */}
        {member || !mainBot || compact ? null : <BotAvatar avatar={mainBot.avatar} name={name} size={56} />}
        <h1 className={clsx('flex items-center gap-2 font-bold text-foreground', compact ? 'text-xl' : 'text-2xl')}>
          {compact && !member && mainBot && <BotAvatar avatar={mainBot.avatar} name={name} size={28} />}
          {member ? draft('下午好，今天想做什么？', 'Good afternoon. What should we work on?') : draft(`下午好，跟${name}说吧`, `Good afternoon. Tell ${name}.`)}
        </h1>
        <p className="text-sm text-muted">
          {member
            ? draft('选一个项目，说清要做的事，Agent 会开一个会话去做。', 'Pick a project and describe the work; an Agent opens a Session for it.')
            : draft(`不用选项目：${name}会自己做，或者开会话、交给合适的伙伴。`, `No project to pick: ${name} does it, or opens a Session and hands it to the right Bot.`)}
        </p>
      </div>
      {!member && <HomeStatusLine />}
      <GettingStarted pending={pending} compact={compact} />
      <div className="flex w-full flex-col gap-3">
        {member && projects && projects.length > 0 && (
          <ProjectPicker projects={projects} targetId={projects[0].id} onSelect={() => {}} onNewProject={() => {}} />
        )}
        <Composer
          layout="stacked"
          stageMedia
          onSend={async () => false}
          placeholder={member ? undefined : draft(`和${name}说…`, `Message ${name}…`)}
          actions={<RouteLabel member={member} />}
        />
      </div>
      <div className="flex w-full flex-col gap-2">
        <span className="px-1 text-xs font-semibold text-muted">{member ? draft('可以这样开始', 'Start with') : draft(`试试这样跟${name}说`, `Try asking ${name}`)}</span>
        <div className={clsx('flex gap-2', compact ? '-mx-4 overflow-x-auto px-4 pb-1' : 'flex-wrap')}>
          {asks.map(({ icon: Icon, label }) => (
            <button
              key={label}
              type="button"
              className="flex shrink-0 items-center gap-2 rounded-full border border-border-strong bg-surface px-3 py-2 text-xs text-foreground transition hover:border-mint/40 hover:bg-mint-soft hover:text-mint-ink"
            >
              <Icon className="size-3.5 text-muted" />
              {label}
            </button>
          ))}
        </div>
      </div>
      {!pending.includes('model') && <TipOfTheDay index={tip} member={member} />}
    </div>
  );
}
