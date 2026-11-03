// Home v3 (owner, 2026-11-03 02:34): the composer is the anchor. It sits at the bottom,
// exactly where a conversation's composer sits, and never moves between states. Above it
// the blocks keep one order (greeting, getting started, tip, suggestions), one width (the
// composer's), and one rule: a block that is absent takes no room, the blocks below it stay
// put, and only the ones above move down. So the page settles toward the composer, never
// piles up at the top. One thing leads at a time: a model while there is none, then the
// composer itself. The composer and the project chips are the shipped ones.
import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  BellRing,
  BookmarkPlus,
  CalendarClock,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Cpu,
  FolderKanban,
  Lightbulb,
  MessagesSquare,
  MousePointer2,
  Rocket,
  Smartphone,
  UserPlus,
  X,
} from 'lucide-react';
import clsx from 'clsx';

import { BotAvatar } from '@/components/ui/bot-avatar';
import { Button } from '@/components/ui/button';
import { BotFace } from '@/components/workbench/bots/BotFace';
import { Composer } from '@/components/workbench/Composer';
import { ProjectPicker } from '@/components/workbench/ProjectPicker';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { useWorkbenchProjectsTree } from '@/context/WorkbenchProjectsContext';

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
  // One line each: the title and its button. `why` is the hover text, not shown inline.
  const rows: Record<SetupGap, { icon: LucideIcon; title: string; why: string; action: string; blocking?: boolean }> = {
    model: {
      icon: Cpu,
      title: draft(`给${name}接上模型`, `Give ${name} a model`),
      why: draft('加一个已有的订阅或 API key。', 'Add a subscription or API key you already have.'),
      action: draft('去添加', 'Add one'),
      blocking: true,
    },
    notifications: {
      icon: BellRing,
      title: draft('打开通知', 'Turn on notifications'),
      why: draft('做完事、需要你决定时第一时间告诉你。', 'Hear as soon as work is done or needs you.'),
      action: draft('打开', 'Turn on'),
    },
    computerUse: {
      icon: MousePointer2,
      title: draft('允许操作电脑', 'Allow Computer Use'),
      why: draft('授予辅助功能和屏幕录制权限。', 'Grant Accessibility and Screen Recording.'),
      action: draft('去授权', 'Grant'),
    },
    chatApps: {
      icon: MessagesSquare,
      title: draft('连接聊天应用', 'Connect a chat app'),
      why: draft(`在 Slack、飞书、微信里也能找${name}。`, `Reach ${name} from Slack, Lark, or WeChat.`),
      action: draft('去连接', 'Connect'),
    },
    phone: {
      icon: Smartphone,
      title: draft('在手机上继续', 'Continue on your phone'),
      why: draft('配对 Avibe Cloud。', 'Pair Avibe Cloud.'),
      action: draft('去配对', 'Pair'),
    },
  };
  return rows;
}

/** What is still missing, one line per item. While there is no model only that item shows,
 *  in the accent; otherwise the first two show, quiet. The rest fold into "N more".
 *  "Later" hides the card until tomorrow; × on an item ("not needed") removes it for good. */
export function GettingStarted({ pending }: { pending: SetupGap[] }) {
  const draft = useDraft();
  const rows = useGapCopy();
  if (pending.length === 0) return null;
  const shown = pending.includes('model') ? (['model'] as SetupGap[]) : pending.slice(0, 2);
  const rest = pending.length - shown.length;
  return (
    <section className="flex w-full flex-col rounded-2xl border border-border bg-surface p-1.5 shadow-mint-card-sm">
      <header className="flex h-8 items-center gap-2 px-2.5">
        <Rocket className="size-4 text-mint-ink" />
        <h2 className="text-sm font-semibold text-foreground">{draft('开始使用', 'Get started')}</h2>
        <span className="flex-1" />
        {rest > 0 && (
          <button type="button" className="inline-flex items-center gap-0.5 text-xs text-muted hover:text-foreground">
            {draft(`还有 ${rest} 项`, `${rest} more`)}
            <ChevronDown className="size-3.5" />
          </button>
        )}
        <button type="button" className="px-1 text-xs text-muted hover:text-foreground">{draft('稍后', 'Later')}</button>
      </header>
      {shown.map((gap) => {
        const { icon: Icon, title, why, action, blocking } = rows[gap];
        return (
          <div key={gap} title={why} className={clsx('flex h-12 items-center gap-3 rounded-xl px-2.5', blocking && 'bg-gold-soft')}>
            <span className={clsx('grid size-8 shrink-0 place-items-center rounded-lg', blocking ? 'bg-surface text-gold-ink' : 'bg-surface-2 text-muted')}>
              <Icon className="size-4" />
            </span>
            <span className={clsx('min-w-0 flex-1 truncate text-sm text-foreground', blocking ? 'font-semibold' : 'font-medium')}>{title}</span>
            <Button type="button" variant={blocking ? 'brand' : 'outline'} size="xs" className="shrink-0 gap-1">
              {action}
              <ChevronRight className="size-3.5" />
            </Button>
            {!blocking && (
              <button type="button" aria-label={draft('不需要', 'Not needed')} title={draft('不需要', 'Not needed')} className="-mr-1 grid size-7 shrink-0 place-items-center rounded-md text-muted hover:bg-foreground/5 hover:text-foreground">
                <X className="size-3.5" />
              </button>
            )}
          </div>
        );
      })}
    </section>
  );
}

/** What is happening, beside the greeting: the Bots at work (faces) and the unread replies.
 *  Each is a way in. On a phone the Inbox tab carries the unread count, so only the faces show. */
export function HomeStatus({ compact = false }: { compact?: boolean }) {
  const draft = useDraft();
  const { bots, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const running = bots.filter((bot) => statusBySession[bot.session_id] === 'running');
  const unread = compact ? 0 : Object.values(unreadBySession).reduce((sum, n) => sum + n, 0);
  if (running.length === 0 && unread === 0) return null;
  return (
    <div className="flex shrink-0 items-center gap-2 text-xs">
      {running.length > 0 && (
        <button type="button" className="inline-flex h-7 items-center gap-1.5 rounded-full border border-mint/30 bg-mint/5 pl-1 pr-2.5">
          <span className="flex -space-x-1.5">
            {running.map((bot) => (
              <BotFace key={bot.handle} avatar={bot.avatar} name={displayName(bot)} size={20} dotWell="bg-surface" />
            ))}
          </span>
          <span className="text-mint-ink">{draft(`${running.length} 位在忙`, `${running.length} working`)}</span>
        </button>
      )}
      {unread > 0 && (
        <button type="button" className="inline-flex h-7 items-center gap-1.5 rounded-full border border-cyan/30 bg-cyan/5 px-2.5">
          <span className="size-1.5 rounded-full bg-cyan-ink" />
          <span className="text-foreground">{draft(`${unread} 条未读`, `${unread} unread`)}</span>
        </button>
      )}
    </div>
  );
}

/** One line: the main Bot, the greeting, and what is happening at the right. No subtitle. */
function Greeting({ member, compact }: { member: boolean; compact: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  const name = mainBot ? displayName(mainBot) : '';
  return (
    <div className={clsx('flex items-center gap-3', compact ? 'h-8' : 'h-10')}>
      {!member && mainBot && <BotAvatar avatar={mainBot.avatar} name={name} size={compact ? 28 : 40} />}
      <h1 className={clsx('min-w-0 flex-1 truncate font-bold text-foreground', compact ? 'text-lg' : 'text-2xl')}>
        {member ? draft('下午好，今天做什么？', 'Good afternoon. What’s next?') : draft(`下午好，跟${name}说吧`, `Good afternoon. Tell ${name}.`)}
      </h1>
      <HomeStatus compact={compact} />
    </div>
  );
}

/** The composer's route: the main Bot and its model, or that there is none yet. A member's shows the Agent. */
function RouteLabel({ member, noModel }: { member: boolean; noModel: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  return (
    <span className="flex items-center gap-1.5 text-xs">
      {!member && mainBot && <BotAvatar avatar={mainBot.avatar} name={displayName(mainBot)} size={18} />}
      <span className="font-semibold text-foreground">{member || !mainBot ? 'vibey' : displayName(mainBot)}</span>
      <span className="font-mono text-muted">{noModel ? draft('· 还没有模型', '· no model yet') : `· gpt-6-astra · ${draft('高', 'high')}`}</span>
    </span>
  );
}

/** Four short labels in equal cells; a tap fills the composer with the whole ask (it never sends). */
function useAsks(member: boolean) {
  const draft = useDraft();
  const asks: { icon: LucideIcon; label: string; ask: string }[] = [
    member
      ? { icon: BookmarkPlus, label: draft('记住一件事', 'Remember this'), ask: draft('记住：周一上午不排会', 'Remember: no meetings Monday mornings') }
      : { icon: UserPlus, label: draft('加一个伙伴', 'Add a Bot'), ask: draft('帮我加一个负责安全的伙伴', 'Add a Bot that owns security') },
    { icon: FolderKanban, label: draft('项目进展如何', 'Project status'), ask: draft('各个项目进展怎么样？', 'How are the projects going?') },
    { icon: Rocket, label: draft('在项目里开工', 'Start in a project'), ask: draft('在 avibe 里把缓存问题修掉', 'Fix the cache bug in avibe') },
    { icon: CalendarClock, label: draft('每周一发周报', 'Weekly report'), ask: draft('每周一早上发我一份周报', 'Send me a report every Monday morning') },
  ];
  return asks;
}

function Asks({ member, compact, quiet }: { member: boolean; compact: boolean; quiet: boolean }) {
  const asks = useAsks(member);
  return (
    <div className={clsx('grid w-full gap-2', compact ? 'grid-cols-2' : 'grid-cols-4')}>
      {asks.map(({ icon: Icon, label, ask }) => (
        <button
          key={label}
          type="button"
          title={ask}
          disabled={quiet}
          className="flex h-11 min-w-0 items-center gap-2 rounded-xl border border-border bg-surface px-3 text-sm text-foreground transition hover:border-mint/40 hover:bg-mint-soft hover:text-mint-ink disabled:opacity-50"
        >
          <Icon className="size-4 shrink-0 text-muted" />
          <span className="truncate">{label}</span>
        </button>
      ))}
    </div>
  );
}

type Tip = { title: string; why: string; member?: boolean };

/** One capability, one sentence. "Try" fills the composer with an ask, or opens where it is set up. */
function useTips(): Tip[] {
  const draft = useDraft();
  return [
    { title: draft('伙伴', 'Bots'), why: draft('给长期的事配一个有名字、有记忆的伙伴。', 'Give an ongoing job a Bot with a name and a memory.'), member: false },
    { title: draft('可视化页面', 'Show Pages'), why: draft('让对话把结果画成一个页面。', 'Have a conversation draw its result as a page.') },
    { title: draft('定时任务和监听', 'Tasks and Watches'), why: draft('到点自动做，或等某件事发生再做。', 'Run on a schedule, or when something happens.') },
    { title: draft('保险库', 'Vault'), why: draft('密钥给 Agent 用，但它看不到原文。', 'Agents use keys without ever seeing them.') },
    { title: draft('技能和 MCP', 'Skills & MCP'), why: draft('装一个，所有 Agent 都多一项本事。', 'Install one and every Agent can use it.') },
    { title: draft('聊天应用', 'Chat apps'), why: draft('在 Slack、飞书、微信里找歪比。', 'Reach Vibey from Slack, Lark, or WeChat.') },
    { title: draft('桌面小窗', 'The desktop pet'), why: draft('在任何应用里按住 ⌥⇧Space 和歪比说话。', 'Hold ⌥⇧Space in any app to talk to Vibey.') },
    { title: draft('语音', 'Voice'), why: draft('手机上按住底栏中间的歪比说话。', 'On a phone, hold Vibey in the tab bar to talk.') },
  ];
}

/** The day's tip: a title, one sentence, and "Try". ‹ › for the others, × hides it until tomorrow.
 *  One line on a desktop; on a phone the controls ride on the title's line. */
export function TipOfTheDay({ index, member = false, compact = false }: { index: number; member?: boolean; compact?: boolean }) {
  const draft = useDraft();
  const tips = useTips().filter((tip) => !member || tip.member !== false);
  const tip = tips[index % tips.length];
  const controls = (
    <span className="flex shrink-0 items-center text-muted">
      <button type="button" aria-label={draft('上一条', 'Previous')} className="grid size-7 place-items-center rounded-md hover:bg-foreground/5 hover:text-foreground"><ChevronLeft className="size-4" /></button>
      <button type="button" aria-label={draft('下一条', 'Next')} className="grid size-7 place-items-center rounded-md hover:bg-foreground/5 hover:text-foreground"><ChevronRight className="size-4" /></button>
      <button type="button" aria-label={draft('今天不再显示', 'Hide for today')} className="grid size-7 place-items-center rounded-md hover:bg-foreground/5 hover:text-foreground"><X className="size-4" /></button>
    </span>
  );
  const tryIt = (
    <Button type="button" variant="outline" size="xs" className="shrink-0">{draft('试试', 'Try')}</Button>
  );
  const mark = (
    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-gold-soft text-gold-ink">
      <Lightbulb className="size-4" />
    </span>
  );
  if (compact) {
    return (
      <section className="flex w-full gap-3 rounded-2xl border border-border bg-surface-2 py-2.5 pl-2.5 pr-1.5">
        {mark}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex h-7 items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground">{tip.title}</span>
            {controls}
          </div>
          <div className="flex items-center gap-2 pr-1">
            <span className="min-w-0 flex-1 text-xs text-muted">{tip.why}</span>
            {tryIt}
          </div>
        </div>
      </section>
    );
  }
  return (
    <section className="flex h-13 w-full items-center gap-3 rounded-2xl border border-border bg-surface-2 pl-2.5 pr-1.5">
      {mark}
      <span className="flex min-w-0 flex-1 items-baseline gap-3 text-sm">
        <span className="shrink-0 font-semibold text-foreground">{tip.title}</span>
        <span className="truncate text-muted">{tip.why}</span>
      </span>
      {tryIt}
      <span className="h-5 w-px bg-border" />
      {controls}
    </section>
  );
}

/** A block of the layout spec: its outline, its number, and when it shows. Off the spec it is just the block. */
function Slot({ spec, label, children }: { spec?: boolean; label: string; children: ReactNode }) {
  if (!spec) return <>{children}</>;
  return (
    <div className="relative mx-auto w-full max-w-(--chat-column)">
      {children}
      <span aria-hidden className="pointer-events-none absolute -inset-1.5 rounded-2xl border-2 border-dashed border-cyan/60" />
      <span className="absolute -top-3 right-4 rounded-full border border-cyan/50 bg-surface px-2 text-xs font-semibold leading-5 text-cyan-ink">{label}</span>
    </div>
  );
}

/** The spec frame's legend: the rules every state follows. */
function SpecLegend() {
  const draft = useDraft();
  const rules = [
    draft('顺序固定：① 问候 → ② 开始使用 → ③ 小技巧 → ④ 试试这样说 → ⑤ 输入框。', 'One order: ① greeting → ② getting started → ③ tip → ④ suggestions → ⑤ composer.'),
    draft('⑤ 固定在底部，和对话页的输入框同一个位置、同一个宽度；内容从它往上排。', '⑤ sits at the bottom, where a conversation’s composer sits, at its width; the blocks stack up from it.'),
    draft('不出现的区块不占位：它下面的区块不动，只有上面的下移。', 'An absent block takes no room: the blocks below stay put; only those above move down.'),
    draft('所有区块和输入框同宽（最宽 1080），左右对齐；区块间距 16，问候下方 24。', 'Every block is the composer’s width (up to 1080), edges aligned; 16 between blocks, 24 under the greeting.'),
    draft('一次一个重点：没有模型时只突出「接上模型」，输入框和建议都弱化；之后重点是输入框。', 'One lead at a time: with no model only that item is in the accent and the rest is quiet; then the composer leads.'),
    draft('手机：同样的顺序和规则；试试这样说 2×2，输入框在底栏上方。', 'Phone: the same order and rules; suggestions 2×2; the composer just above the tab bar.'),
  ];
  return (
    <section className="mb-auto flex flex-col gap-2 rounded-2xl border border-dashed border-cyan/50 bg-surface/80 p-4">
      <h2 className="text-sm font-semibold text-cyan-ink">{draft('首页版面结构', 'Home layout')}</h2>
      <ol className="flex flex-col gap-1 text-xs text-foreground">
        {rules.map((rule) => <li key={rule}>{rule}</li>)}
      </ol>
    </section>
  );
}

/** The whole home. `pending` is the setup still missing; `tip` which tip is up, or null when
 *  hidden for today; `member` a reader without Bots; `compact` a phone; `spec` outlines the blocks.
 *  The tip waits while there is no model: until the main Bot can work, teaching more is noise. */
export function HomePage({ pending, tip, member = false, compact = false, spec = false }: { pending: SetupGap[]; tip: number | null; member?: boolean; compact?: boolean; spec?: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  const { projects } = useWorkbenchProjectsTree();
  const name = mainBot ? displayName(mainBot) : '';
  const noModel = pending.includes('model');
  const showTip = tip !== null && !noModel;
  return (
    // A desktop page undoes the shell's padding as a conversation does, so the composer lands where its composer does.
    <div className={clsx('flex flex-col', compact ? 'min-h-0 flex-1' : '-mx-10 -my-8 h-(--app-vvh)')}>
      <div className={clsx('min-h-0 flex-1 overflow-y-auto', compact ? 'px-4 pt-5' : 'px-8 pt-8')}>
        <div className="mx-auto flex min-h-full w-full max-w-(--chat-column) flex-col justify-end gap-4 pb-1">
          {spec && <SpecLegend />}
          <Slot spec={spec} label={draft('① 问候 · 总在', '① Greeting · always')}>
            <div className="pb-2"><Greeting member={member} compact={compact} /></div>
          </Slot>
          {pending.length > 0 && (
            <Slot spec={spec} label={draft('② 开始使用 · 有未完成项才出现', '② Getting started · while something is missing')}>
              <GettingStarted pending={pending} />
            </Slot>
          )}
          {showTip && (
            <Slot spec={spec} label={draft('③ 小技巧 · 有模型且今天没关掉', '③ Tip · with a model, unless hidden today')}>
              <TipOfTheDay index={tip} member={member} compact={compact} />
            </Slot>
          )}
          <Slot spec={spec} label={draft('④ 试试这样说 · 总在', '④ Suggestions · always')}>
            <Asks member={member} compact={compact} quiet={noModel} />
          </Slot>
          {member && projects && projects.length > 0 && (
            <ProjectPicker projects={projects} targetId={projects[0].id} onSelect={() => {}} onNewProject={() => {}} />
          )}
        </div>
      </div>
      {/* The conversation composer's own padding: this is the line it rests on. On a phone it clears the raised center of the tab bar. */}
      <div className={clsx('shrink-0', compact ? 'px-4 pb-4 pt-3' : 'px-8 pb-4 pt-3')}>
        <Slot spec={spec} label={draft('⑤ 输入框 · 总在，位置同对话页', '⑤ Composer · always, where a conversation’s is')}>
          <Composer
            layout="stacked"
            stageMedia
            onSend={async () => false}
            disabled={noModel}
            placeholder={noModel ? draft(`先给${name}接上模型，就能和它说话`, `Give ${name} a model first, then talk to it`) : member ? undefined : draft(`和${name}说…`, `Message ${name}…`)}
            actions={<RouteLabel member={member} noModel={noModel} />}
          />
        </Slot>
      </div>
    </div>
  );
}
