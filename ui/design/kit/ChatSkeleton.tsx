// A conversation opened before its messages arrived (ui/design: chat/skeleton/*). Everything the
// app already knows is drawn as itself, so nothing appears when the bootstrap lands: the header with
// the Session's title, its ⋯ menu and its Show Page control, and the composer with Attach, the
// Agent's route (agent · model · effort), and Send. Only the transcript is blocks: the user's bubbles
// on the right, the Agent's on the left under its name, each with the time line the transcript keeps
// under a bubble. `latest`: opened from the Inbox, the newest Agent reply is known (its row's
// preview), so it is drawn as the real bubble at the bottom and only older messages are blocks.
// ChatPage belongs to another lane; this is the look it is asked to build. The header, bubble,
// composer, and picker are the shipped pieces, laid out the way ChatPage lays them.
import { Archive, ArrowLeft, GitFork, Hash, Pin } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Markdown } from '@/components/ui/markdown';
import { Skeleton, SkeletonShimmer } from '@/components/ui/skeleton';
import { BackendAvatar, type BackendId } from '@/components/visual/BackendIcon';
import { AgentRoutePicker } from '@/components/workbench/AgentRoutePicker';
import { AGENT_BUBBLE } from '@/components/workbench/chatBubble';
import { Composer } from '@/components/workbench/Composer';
import { RoleAvatar } from '@/components/workbench/RoleAvatar';
import { ChatSessionActionMenu, type SessionActionDescriptor } from '@/components/workbench/sessionActions';
import { ShowPageLaunchControl } from '@/components/workbench/ShowPageLaunchControl';
import type { VibeAgentBrief, WorkbenchSession } from '@/context/ApiContext';

const noop = () => {};

// Each bubble: which side, its width in the column, and its lines (the last one shorter, as text wraps).
type BubbleShape = { side: 'user' | 'agent'; width: string; lines: string[] };
const OPENING: BubbleShape[] = [
  { side: 'user', width: 'w-[min(22rem,72%)]', lines: ['w-full', 'w-[58%]'] },
  { side: 'agent', width: 'w-[min(44rem,88%)]', lines: ['w-full', 'w-[94%]', 'w-[86%]', 'w-[48%]'] },
  { side: 'user', width: 'w-[min(14rem,52%)]', lines: ['w-[88%]'] },
  { side: 'agent', width: 'w-[min(38rem,84%)]', lines: ['w-full', 'w-[90%]', 'w-[40%]'] },
];
// Above a known latest reply: older messages, the newest of them last (a user message, which the
// reply answers).
const OLDER: BubbleShape[] = [
  { side: 'agent', width: 'w-[min(40rem,86%)]', lines: ['w-full', 'w-[92%]', 'w-[54%]'] },
  { side: 'user', width: 'w-[min(18rem,64%)]', lines: ['w-full', 'w-[40%]'] },
  { side: 'agent', width: 'w-[min(36rem,82%)]', lines: ['w-full', 'w-[70%]'] },
  { side: 'user', width: 'w-[min(20rem,70%)]', lines: ['w-[84%]'] },
];

/** The Agent's avatar and name above its bubble, as the transcript draws them. */
function AgentHead({ backend, name }: { backend: string; name: string }) {
  return (
    <div className="flex items-center gap-2 px-0.5">
      <RoleAvatar tone="mint">
        <BackendAvatar backend={backend as BackendId} />
      </RoleAvatar>
      <span className="text-[11px] font-medium text-muted">{name}</span>
    </div>
  );
}

function BubbleSkeleton({ side, width, lines }: BubbleShape) {
  return (
    <div className={clsx('flex w-full', side === 'user' ? 'justify-end' : 'justify-start')}>
      <div className={clsx('flex max-w-[min(92%,860px)] flex-col gap-1', side === 'user' ? 'items-end' : 'items-start', width)}>
        {side === 'agent' && (
          <div className="flex items-center gap-2 px-0.5">
            <Skeleton className="size-6 rounded-full" />
            <span className="flex h-[1lh] items-center text-[11px]">
              <Skeleton className="h-1.5 w-10" />
            </span>
          </div>
        )}
        <div
          className={clsx(
            'flex w-full flex-col rounded-2xl border border-transparent bg-foreground/[0.04] px-3.5 py-2.5 text-[14px] leading-[1.6]',
            side === 'user' ? 'rounded-tr-md' : 'rounded-tl-md',
          )}
        >
          {lines.map((line, index) => (
            <span key={index} className="flex h-[1lh] items-center">
              <Skeleton className={clsx('h-2', line)} />
            </span>
          ))}
        </div>
        {/* The time line under each bubble: shown on touch screens, kept as space elsewhere. */}
        <span className="flex h-[1lh] items-center px-1 font-mono text-[10px]">
          <Skeleton className="h-1.5 w-16 opacity-0 pointer-coarse:opacity-100" />
        </span>
      </div>
    </div>
  );
}

/** The ⋯ menu's rows for an ordinary Session (`useSessionActions`), inert here. */
function useHeaderActions(): SessionActionDescriptor[] {
  const { t } = useTranslation();
  return [
    { id: 'pin', group: 'organize', icon: Pin, label: t('workbench.sessionPin'), onSelect: noop },
    { id: 'reference', group: 'continue', icon: Hash, label: t('workbench.sessionReference'), onSelect: noop },
    { id: 'fork', group: 'continue', icon: GitFork, label: t('workbench.sessionFork'), onSelect: noop },
    { id: 'archive', group: 'lifecycle', icon: Archive, label: t('workbench.sessionArchive'), danger: true, onSelect: noop },
  ];
}

export function ChatSkeleton({
  session,
  agents,
  latest,
}: {
  /** The Session row, known from the list it was opened from. */
  session: WorkbenchSession;
  agents: VibeAgentBrief[];
  /** The newest Agent reply, known from the Inbox row the chat was opened from. */
  latest?: string;
}) {
  const { t } = useTranslation();
  const actions = useHeaderActions();
  const backend = session.agent_backend ?? '';
  return (
    <div className="fixed inset-x-0 bottom-0 top-0 z-40 flex flex-col bg-background pt-[env(safe-area-inset-top)] md:relative md:inset-auto md:z-auto md:-mx-10 md:-my-8 md:h-[var(--app-vvh)] md:bg-transparent md:pt-0">
      {/* The header bar (ChatHeaderBar): back, title, the session menu, and the Show Page control. */}
      <div className="shrink-0 border-b border-border bg-surface/70 px-4 py-2.5 backdrop-blur md:px-8">
        <div className="mx-auto flex w-full max-w-[1080px] items-center gap-3">
          <Button type="button" variant="outline" size="icon" aria-label={t('chat.back')} className="size-7 shrink-0">
            <ArrowLeft className="size-3.5" />
          </Button>
          <button
            type="button"
            className="-mx-1.5 inline-flex min-w-0 items-center truncate rounded-md px-1.5 py-0.5 text-left text-[16px] font-bold text-foreground"
          >
            <span className="truncate">{session.title || t('chat.untitled')}</span>
          </button>
          <ChatSessionActionMenu actions={actions} label={t('workbench.sessionActions')} viewport="wide" />
          <div className="ml-auto flex items-center gap-1.5">
            <ShowPageLaunchControl
              sessionId={session.id}
              title={session.title}
              showPageMode={false}
              busy={false}
              onToggle={noop}
              onPrepareLaunch={async () => true}
            />
            <ChatSessionActionMenu actions={actions} label={t('workbench.sessionActions')} viewport="compact" />
          </div>
        </div>
      </div>
      <SkeletonShimmer className="min-h-0 flex-1 px-4 py-5 md:px-8">
        <div className={clsx('mx-auto flex w-full max-w-[1080px] flex-col gap-3', latest && 'h-full justify-end')}>
          {(latest ? OLDER : OPENING).map((bubble, index) => (
            <BubbleSkeleton key={index} {...bubble} />
          ))}
          {latest && (
            <div className="flex w-full justify-start">
              <div className="flex max-w-[min(92%,860px)] flex-col items-start gap-1">
                <AgentHead backend={backend} name={session.agent_name ?? ''} />
                <div className={AGENT_BUBBLE} style={{ fontSize: '14px' }}>
                  <Markdown content={latest} className="vr-markdown--inherit-size" />
                </div>
                <span className="h-[1lh] px-1 font-mono text-[10px]" />
              </div>
            </div>
          )}
        </div>
      </SkeletonShimmer>
      {/* The composer bar (Compose): Attach, the route picker, and Send, as the chat draws them. */}
      <div className="shrink-0 bg-linear-to-t from-background from-65% to-transparent px-4 pb-[calc(1rem+env(safe-area-inset-bottom))] pt-3 md:px-8 md:pb-4">
        <Composer
          sessionId={session.id}
          layout="stacked"
          onSend={noop}
          actions={
            <AgentRoutePicker
              presentation="compact"
              value={session}
              agents={agents}
              onChange={noop}
              triggerClassName="max-w-[min(60vw,360px)]"
              align="end"
              speed={session.speed ?? null}
              onSpeedChange={noop}
            />
          }
        />
      </div>
    </div>
  );
}
