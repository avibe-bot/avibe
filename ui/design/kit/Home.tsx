// Home v4 (owner, 2026-11-03 12:02): one centered group, not a chat page. From top to bottom the
// blocks keep one order (greeting, getting started, tip, suggestions, composer), one width, and one
// gap between any two of them; the composer is the group's last block, never pinned to the bottom.
// The group sits in the middle of the page, a little above it (the space above and below it is split
// 2 : 3), so a block that appears or goes away re-centers the whole group without changing its order.
// One thing leads at a time: a model while there is none; after that the suggestions and the tip
// invite a first try. The composer and the project chips are the shipped ones.
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

import vibeyMascot from '@/assets/pet/vibey.png';
import { BotAvatar } from '@/components/ui/bot-avatar';
import { Button } from '@/components/ui/button';
import { AgentRoutePicker } from '@/components/workbench/AgentRoutePicker';
import { BotFace } from '@/components/workbench/bots/BotFace';
import { Composer } from '@/components/workbench/Composer';
import type { Bot } from '@/lib/bots';
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
      why: draft('用已有的订阅或 API key', 'A subscription or API key you have'),
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

/** What is still missing. While there is no model only that item shows, and it leads: the one
 *  accented row on the page, with a short line on what it takes. Otherwise the first two show
 *  (one on a phone), quiet, one line each. The rest fold into "N more". "Later" hides the card
 *  until tomorrow; × on an item ("not needed") removes it for good. */
export function GettingStarted({ pending, compact = false }: { pending: SetupGap[]; compact?: boolean }) {
  const draft = useDraft();
  const rows = useGapCopy();
  if (pending.length === 0) return null;
  const shown = pending.includes('model') ? (['model'] as SetupGap[]) : pending.slice(0, compact ? 1 : 2);
  const rest = pending.length - shown.length;
  return (
    <section className="flex w-full flex-col rounded-2xl border border-border bg-surface p-1.5 shadow-mint-card-sm">
      <header className={clsx('flex items-center gap-2 px-2.5', compact ? 'h-7' : 'h-8')}>
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
          <div key={gap} title={blocking ? undefined : why} className={clsx('flex items-center gap-3 rounded-xl px-2.5', blocking ? 'bg-gold-soft py-3' : compact ? 'h-11' : 'h-12')}>
            <span className={clsx('grid shrink-0 place-items-center rounded-lg', blocking ? 'size-10 bg-surface text-gold-ink' : 'size-8 bg-surface-2 text-muted')}>
              <Icon className={blocking ? 'size-5' : 'size-4'} />
            </span>
            {blocking ? (
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="truncate text-base font-semibold text-foreground">{title}</span>
                <span className={clsx('text-xs text-muted', !compact && 'truncate')}>{why}</span>
              </span>
            ) : (
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">{title}</span>
            )}
            {blocking ? (
              <Button type="button" variant="brand" size="sm" className="shrink-0 gap-1">
                {action}
                <ChevronRight className="size-3.5" />
              </Button>
            ) : (
              <TextButton>{action}</TextButton>
            )}
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

/** The main Bot's face, the greeting, and, below it, what is happening (only when something is). No subtitle.
 *  Centered: it opens the group. Before there is a model the main Bot introduces itself, since it cannot
 *  answer yet; after that it asks what to do. On a phone the face is left out: the tab bar's center is it. */
function Greeting({ member, compact, noModel }: { member: boolean; compact: boolean; noModel: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  const name = mainBot ? displayName(mainBot) : '';
  return (
    <div className={clsx('flex flex-col items-center text-center', compact ? 'gap-2' : 'gap-3')}>
      {!member && !compact && mainBot && <StandaloneFace bot={mainBot} name={name} />}
      <h1 className={clsx('font-bold text-foreground', compact ? 'text-2xl' : 'text-3xl')}>
        {noModel && name ? draft(`下午好，我是${name}`, `Good afternoon. I’m ${name}.`) : draft('下午好，今天做什么？', 'Good afternoon. What’s next?')}
      </h1>
      <HomeStatus compact={compact} />
    </div>
  );
}

/** A small action on a card: text only, no border or fill; hovering lights a soft wash. The one
 *  lead on the page is the only solid button. */
function TextButton({ children, chevron = true }: { children: ReactNode; chevron?: boolean }) {
  return (
    <button
      type="button"
      className="inline-flex h-7 shrink-0 items-center gap-0.5 rounded-md px-2 text-xs font-semibold text-mint-ink transition hover:bg-mint-soft"
    >
      {children}
      {chevron && <ChevronRight className="size-3.5" />}
    </button>
  );
}

/** A face standing alone, as #3192 draws one (`BotAvatar plain`): the mascot straight on the page,
 *  without its well and hairline, and a faint shadow in light. Until #3192 ships its
 *  `--bot-mascot-plain-shadow`, the mascot's own shadow stands in; then this becomes `<BotAvatar plain />`.
 *  Any other avatar is its own disc. */
function StandaloneFace({ bot, name }: { bot: Bot; name: string }) {
  if (bot.avatar?.kind !== 'mascot') return <BotAvatar avatar={bot.avatar} name={name} size={64} />;
  return (
    <span className="grid size-16 shrink-0 place-items-center" aria-hidden="true">
      <img src={vibeyMascot} alt="" draggable={false} className="size-full scale-[1.12] object-contain [filter:var(--bot-mascot-shadow)]" />
    </span>
  );
}

/** The composer's route, as in a Bot's conversation: the shipped picker with the Agent locked, so it
 *  shows the model and effort alone (the placeholder already says who listens). A member's names the
 *  Agent. With no model there is no route yet: the way to add one takes its place, to the same page
 *  as "Add one" in Get started (Settings · Models). */
function ComposerRoute({ member, noModel }: { member: boolean; noModel: boolean }) {
  const draft = useDraft();
  if (noModel) return <TextButton>{draft('去接模型', 'Add a model')}</TextButton>;
  return (
    <AgentRoutePicker
      presentation="compact"
      agentLocked={!member}
      value={{ agent_name: 'vibey', agent_backend: 'vibey', model: 'gpt-6-astra', reasoning_effort: 'high' }}
      agents={[]}
      onChange={() => {}}
    />
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
          className={clsx(
            'flex min-w-0 items-center gap-2.5 rounded-xl border border-border bg-surface px-3 text-sm text-foreground transition hover:border-mint/40 hover:bg-mint-soft hover:text-mint-ink disabled:opacity-50',
            compact ? 'h-11' : 'h-12',
          )}
        >
          <span className={clsx('grid size-7 shrink-0 place-items-center rounded-lg', quiet ? 'bg-surface-2 text-muted' : 'bg-mint-soft text-mint-ink')}>
            <Icon className="size-4" />
          </span>
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
 *  One line on a desktop; on a phone the title over the sentence, and a swipe for the others. */
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
    <TextButton chevron={false}>{draft('试试', 'Try')}</TextButton>
  );
  const mark = (
    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-gold-soft text-gold-ink">
      <Lightbulb className="size-4" />
    </span>
  );
  if (compact) {
    // A phone swipes the card for the other tips, so only × stays beside "Try".
    return (
      <section className="flex w-full items-center gap-3 rounded-2xl border border-border bg-surface-2 py-2.5 pl-2.5 pr-1">
        {mark}
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-sm font-semibold text-foreground">{tip.title}</span>
          <span className="text-xs text-muted">{tip.why}</span>
        </span>
        <span className="flex shrink-0 items-center gap-0.5">
          {tryIt}
          <button type="button" aria-label={draft('今天不再显示', 'Hide for today')} className="grid size-7 place-items-center rounded-md text-muted hover:bg-foreground/5 hover:text-foreground"><X className="size-4" /></button>
        </span>
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

/** A block of the layout spec: its outline, its number, and when it shows; above it, the gap to the
 *  block before. Off the spec it is just the block. */
function Slot({ spec, label, gap, compact = false, children }: { spec?: boolean; label: string; gap?: string; compact?: boolean; children: ReactNode }) {
  if (!spec) return <>{children}</>;
  return (
    <div className="relative w-full">
      {children}
      <span aria-hidden className="pointer-events-none absolute -inset-1 rounded-2xl border-2 border-dashed border-cyan/60" />
      <span className="absolute -top-2.5 right-4 rounded-full border border-cyan/50 bg-surface px-2 text-xs font-semibold leading-4 text-cyan-ink">{label}</span>
      {gap && (
        <span className={clsx('absolute bottom-full left-4 z-10 flex items-center', compact ? 'h-4' : 'h-7')}>
          <span className="rounded-full border border-violet/50 bg-surface px-2 text-xs font-semibold leading-4 text-violet-ink">{gap}</span>
        </span>
      )}
    </div>
  );
}

/** The free space above or below the group: 2 parts above, 3 below, so the group sits a little
 *  above the middle. It shrinks first when the window is short (to 24, 8 on a phone), and then
 *  the page scrolls. With the keyboard up nothing re-centers: the page moves up whole. */
function Spacer({ part, compact, spec, label }: { part: 'above' | 'below'; compact: boolean; spec?: boolean; label?: string }) {
  return (
    <div className={clsx('relative shrink', compact ? 'min-h-2' : 'min-h-6', part === 'above' ? 'grow-2' : 'grow-3')}>
      {spec && (
        <span className={clsx('absolute inset-y-2 inset-x-0 grid place-items-center rounded-2xl border-2 border-dashed border-violet/50 bg-violet/5', !compact && 'mx-auto max-w-3xl')}>
          <span className="rounded-full border border-violet/40 bg-surface px-2 text-xs font-semibold leading-5 text-violet-ink">{label}</span>
        </span>
      )}
    </div>
  );
}

/** The spec frame's legend: the rules every state follows, on a desktop and on a phone. */
function SpecLegend() {
  const draft = useDraft();
  const rules = [
    draft('顺序固定：① 问候 → ② 开始使用 → ③ 小技巧 → ④ 试试这样说 → ⑤ 输入框。输入框是这一组的最后一块，不钉在页面底部。', 'One order: ① greeting → ② getting started → ③ tip → ④ suggestions → ⑤ composer. The composer is the group’s last block, not pinned to the page’s bottom.'),
    draft('整组居中、略偏上：上下留白按 2 : 3 分。区块出现或消失，整组重新居中，顺序不变。', 'The group is centered, a little high: the free space splits 2 : 3. A block that comes or goes re-centers the group; the order stays.'),
    draft('一种间距：桌面 28，手机 16，任何两块之间都一样。窗口不够高时先收留白（桌面至少 24，手机 8），再让页面滚动；间距不压缩。', 'One gap: 28 on a desktop, 16 on a phone, between any two blocks. When the window is short the free space shrinks first (24 on a desktop, 8 on a phone at least), then the page scrolls; the gap never shrinks.'),
    draft('一种宽度：所有区块和输入框同宽（桌面最宽 768，大窗口也不放大；手机两边各留 16），左右对齐。', 'One width: every block and the composer (768 at most on a desktop, a large window included; the screen less 16 a side on a phone), edges aligned.'),
    draft('一次一个重点：没有模型时只突出「接上模型」，问候改成自我介绍，建议变灰，输入框禁用，它的路由处给「去接模型」，和「去添加」去同一处（设置 · 模型）。有了模型，问候问「今天做什么」，建议和小技巧邀请你试一次。', 'One lead: with no model only that item is accented, the greeting introduces the Bot, the suggestions are quiet, and the composer is disabled, with “Add a model” where its route goes, to the same page as “Add one” (Settings · Models). With a model the greeting asks what’s next, and the suggestions and tip invite a first try.'),
    draft('按钮：只有唯一的重点是实心主按钮；卡片上的小按钮（「打开」「去授权」「试试」）是文字按钮，没有边框和底色，悬停出浅底。输入框的路由和伙伴对话一样，只显示模型和推理强度。', 'Buttons: only the one lead is a solid button; a card’s small actions (Turn on, Grant, Try) are text, with no border or fill, and a soft wash on hover. The composer’s route shows the model and effort alone, as in a Bot’s conversation.'),
    draft('手机：问候不放头像（底栏中间就是它）；「开始使用」只露 1 项。成员：项目选择紧贴输入框上方（间距 8），和输入框算一块。', 'A phone: no face in the greeting (the tab bar’s center is it); getting started shows one item. A member: the project chips sit right above the composer (8 apart), one block with it.'),
    draft('手机键盘弹起：整组不按变矮的可见区域重新居中，页面整体上移，让输入框贴在键盘上方；头部和上面的区块移出屏幕，底栏被键盘盖住。', 'A phone keyboard: the group does not re-center in the shorter visible area; the page moves up whole so the composer rests on the keyboard; the header and the blocks above move off screen, the keyboard covers the tab bar.'),
  ];
  return (
    <section className="absolute right-8 top-8 flex w-80 flex-col gap-2 rounded-2xl border border-dashed border-cyan/50 bg-surface/90 p-4">
      <h2 className="text-sm font-semibold text-cyan-ink">{draft('首页版面结构', 'Home layout')}</h2>
      <ol className="flex list-decimal flex-col gap-1.5 pl-4 text-xs leading-relaxed text-foreground">
        {rules.map((rule) => <li key={rule}>{rule}</li>)}
      </ol>
    </section>
  );
}

/** The whole home. `pending` is the setup still missing; `tip` which tip is up, or null when
 *  hidden for today; `member` a reader without Bots; `compact` a phone; `spec` outlines the blocks,
 *  the gaps, and the free space; `keyboard` a phone with the keyboard up, the page moved up whole
 *  until the composer rests on it. The tip waits while there is no model: until the main Bot can
 *  work, teaching more is noise. With no model the composer is disabled and offers the way to add one (owner, 2026-11-03). */
export function HomePage({
  pending,
  tip,
  member = false,
  compact = false,
  spec = false,
  keyboard = false,
}: {
  pending: SetupGap[];
  tip: number | null;
  member?: boolean;
  compact?: boolean;
  spec?: boolean;
  keyboard?: boolean;
}) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  const { projects } = useWorkbenchProjectsTree();
  const name = mainBot ? displayName(mainBot) : '';
  const noModel = pending.includes('model');
  const showTip = tip !== null && !noModel;
  const gap = compact ? '16' : '28';
  return (
    // The page fills the space beside the sidebar (on a phone, between the header and the tab bar) and scrolls
    // only when the group is taller than it. With the keyboard up the frame shows it moved up: its end on the keyboard.
    <div
      data-home-page
      className={clsx(
        'relative flex min-h-0 flex-col',
        keyboard ? 'justify-end overflow-hidden' : 'overflow-y-auto',
        compact ? 'flex-1 px-4' : 'h-full',
      )}
    >
      {spec && !compact && <SpecLegend />}
      {!keyboard && <Spacer part="above" compact={compact} spec={spec} label={draft('上方留白 · 2 份', 'Free space above · 2 parts')} />}
      <div data-home-group className={clsx('mx-auto flex w-full shrink-0 flex-col', compact ? 'gap-4' : 'max-w-3xl gap-7')}>
        <Slot spec={spec} label={draft('① 问候 · 总在', '① Greeting · always')}>
          <Greeting member={member} compact={compact} noModel={noModel} />
        </Slot>
        {pending.length > 0 && (
          <Slot spec={spec} compact={compact} gap={gap} label={draft('② 开始使用 · 有未完成项才出现', '② Getting started · while something is missing')}>
            <GettingStarted pending={pending} compact={compact} />
          </Slot>
        )}
        {showTip && (
          <Slot spec={spec} compact={compact} gap={gap} label={draft('③ 小技巧 · 有模型且今天没关掉', '③ Tip · with a model, unless hidden today')}>
            <TipOfTheDay index={tip} member={member} compact={compact} />
          </Slot>
        )}
        <Slot spec={spec} compact={compact} gap={gap} label={draft('④ 试试这样说 · 总在', '④ Suggestions · always')}>
          <Asks member={member} compact={compact} quiet={noModel} />
        </Slot>
        <Slot spec={spec} compact={compact} gap={gap} label={draft('⑤ 输入框 · 总在，最后一块', '⑤ Composer · always, the last block')}>
          <div className="flex flex-col gap-2">
            {member && projects && projects.length > 0 && (
              <ProjectPicker projects={projects} targetId={projects[0].id} onSelect={() => {}} onNewProject={() => {}} />
            )}
            <Composer
              layout="stacked"
              stageMedia
              onSend={async () => false}
              disabled={noModel}
              placeholder={noModel ? draft('接上模型后就能说话', 'Talk once it has a model') : member ? undefined : draft(`和${name}说…`, `Message ${name}…`)}
              actions={<ComposerRoute member={member} noModel={noModel} />}
            />
          </div>
        </Slot>
      </div>
      {keyboard ? <div className="h-4 shrink-0" /> : <Spacer part="below" compact={compact} spec={spec} label={draft('下方留白 · 3 份', 'Free space below · 3 parts')} />}
    </div>
  );
}
