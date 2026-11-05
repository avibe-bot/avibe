// A conversation opened before its messages arrived (ui/design: chat/skeleton/*). Everything the
// app already knows is drawn as itself, so nothing appears when the bootstrap lands: the header with
// the Session's title, its ⋯ menu and its Show Page control, and the composer with Attach, the
// Agent's route (agent · model · effort), and Send. Only the transcript is blocks: the user's bubbles
// on the right, the Agent's on the left under its name, each with the time line the transcript keeps
// under a bubble. `latest`: opened from the Inbox, the newest Agent reply is known (its row's
// preview), so it is drawn as the real bubble at the bottom and only older messages are blocks.
// ChatPage belongs to another lane; this is the look it is asked to build. The header is the shipped
// `ChatHeaderBar`; the bubble, composer, and picker are the shipped pieces, laid out the way ChatPage
// lays them. Bubble sizes are Tailwind's scale (a fraction of the column, capped on the spacing scale),
// and the column and message widths are `--chat-column-w` and `--chat-message-max-w`.
import { Archive, GitFork, Hash, Pin } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Markdown } from '@/components/ui/markdown';
import { Skeleton, SkeletonShimmer } from '@/components/ui/skeleton';
import { BackendAvatar, type BackendId } from '@/components/visual/BackendIcon';
import { AgentRoutePicker } from '@/components/workbench/AgentRoutePicker';
import { ChatHeaderBar } from '@/components/workbench/ChatPage';
import { AGENT_BUBBLE } from '@/components/workbench/chatBubble';
import { Composer } from '@/components/workbench/Composer';
import { RoleAvatar } from '@/components/workbench/RoleAvatar';
import type { SessionActionDescriptor } from '@/components/workbench/sessionActions';
import type { VibeAgentBrief, WorkbenchSession } from '@/context/ApiContext';

const noop = () => {};
const inertAnnotation = {
  state: null,
  setIframe: noop,
  handleIframeLoad: noop,
  handleShortcutKeyDown: noop,
  enable: noop,
  disable: noop,
  setMode: noop,
};

// Each bubble: which side, its width in the column, and its lines (the last one shorter, as text wraps).
type BubbleShape = { side: 'user' | 'agent'; width: string; lines: string[] };
const OPENING: BubbleShape[] = [
  { side: 'user', width: 'w-18/25 max-w-88', lines: ['w-full', 'w-29/50'] },
  { side: 'agent', width: 'w-22/25 max-w-176', lines: ['w-full', 'w-47/50', 'w-43/50', 'w-12/25'] },
  { side: 'user', width: 'w-13/25 max-w-56', lines: ['w-22/25'] },
  { side: 'agent', width: 'w-21/25 max-w-152', lines: ['w-full', 'w-9/10', 'w-2/5'] },
];
// Above a known latest reply: older messages, the newest of them last (a user message, which the
// reply answers).
const OLDER: BubbleShape[] = [
  { side: 'agent', width: 'w-43/50 max-w-160', lines: ['w-full', 'w-23/25', 'w-27/50'] },
  { side: 'user', width: 'w-16/25 max-w-72', lines: ['w-full', 'w-2/5'] },
  { side: 'agent', width: 'w-41/50 max-w-144', lines: ['w-full', 'w-7/10'] },
  { side: 'user', width: 'w-7/10 max-w-80', lines: ['w-21/25'] },
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
      <div className={clsx('flex flex-col gap-1', side === 'user' ? 'items-end' : 'items-start', width)}>
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
  const actions = useHeaderActions();
  const backend = session.agent_backend ?? '';
  return (
    <div className="fixed inset-x-0 bottom-0 top-0 z-40 flex flex-col bg-background pt-[env(safe-area-inset-top)] md:relative md:inset-auto md:z-auto md:-mx-10 md:-my-8 md:h-[var(--app-vvh)] md:bg-transparent md:pt-0">
      {/* The header: the shipped bar, with what the Session row already says. */}
      <ChatHeaderBar
        session={session}
        agents={agents}
        defaultAgentName={null}
        onPatch={noop}
        onBack={noop}
        showPageMode={false}
        showPageBusy={false}
        onToggleShowPage={noop}
        onPrepareShowPageLaunch={() => Promise.resolve(true)}
        annotation={inertAnnotation}
        readOnlyReason={null}
        sessionActions={actions}
      />
      <SkeletonShimmer className="min-h-0 flex-1 px-4 py-5 md:px-8">
        <div className={clsx('mx-auto flex w-full max-w-(--chat-column-w) flex-col gap-3', latest && 'h-full justify-end')}>
          {(latest ? OLDER : OPENING).map((bubble, index) => (
            <BubbleSkeleton key={index} {...bubble} />
          ))}
          {latest && (
            <div className="flex w-full justify-start">
              <div className="flex max-w-(--chat-message-max-w) flex-col items-start gap-1">
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
