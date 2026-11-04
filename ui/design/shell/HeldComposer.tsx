// A conversation's composer in one moment of a hold (ui/design: composer/hold-*). The chat page
// and its composer are the shipped ones; the hold's parts (the card, the box while held, the dim)
// are the shipped `ComposerHold` views, laid exactly over the shipped composer the way the
// composer lays them over itself, in the state `hold` describes (as `useHoldToTalk` reports it).
// The operating system's pointer, keyboard, and edit menu are stand-ins, not product UI.
import type { ReactNode } from 'react';
import { MousePointer2, TextCursor } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import {
  ComposerHoldCard,
  ComposerHoldSurface,
  type ComposerHoldView,
  type HoldPointer,
} from '@/components/voice/ComposerHold';
import type { VoiceTarget } from '@/components/voice/VoiceCard';
import { Composer } from '@/components/workbench/Composer';
import { useBots } from '@/context/BotsContext';
import { meterFor } from './meter';

/** Who a frame's composer talks to: a Bot by handle, or a Session by its title. */
export type HeldTarget = { bot: string } | { session: string };

function useTarget(to: HeldTarget): VoiceTarget {
  const { t } = useTranslation();
  const { bots, displayName } = useBots();
  if ('session' in to) return { name: to.session, title: t('voice.composer.toSession', { title: to.session }) };
  const bot = bots.find((each) => each.handle === to.bot);
  return bot ? { name: displayName(bot), avatar: bot.avatar } : { name: to.bot };
}

/** The composer's box, measured by an invisible shipped Composer placed where the chat page puts
 *  its own (Compose's padding), so what a frame draws in it lies exactly over the shipped one. */
function ComposerSlot({ children }: { children?: ReactNode }) {
  return (
    <div className="relative shrink-0 px-4 pt-3 pb-4 md:px-8">
      <div className="grid">
        <div className="invisible col-start-1 row-start-1" aria-hidden="true">
          <Composer layout="stacked" onSend={() => {}} />
        </div>
        <div className="relative col-start-1 row-start-1 mx-auto w-full max-w-(--chat-column-w)">{children}</div>
      </div>
    </div>
  );
}

const noop = () => {};

export function HeldComposer({
  pointer,
  to,
  hold,
  attachments,
  point = { x: 360, y: 26 },
}: {
  pointer: HoldPointer;
  to: HeldTarget;
  hold: Partial<ComposerHoldView>;
  attachments?: string[];
  /** Where a mouse is pressed in the box (the ring, and the stand-in pointer). */
  point?: { x: number; y: number };
}) {
  const target = useTarget(to);
  const view: ComposerHoldView = {
    phase: 'idle',
    interruptReason: null,
    text: '',
    stash: '',
    seconds: 0,
    progress: 0,
    outcome: null,
    canRetry: true,
    sendInterrupted: noop,
    retry: noop,
    discard: noop,
    dismiss: noop,
    ...hold,
    level: hold.level ?? meterFor(hold.phase ?? 'idle'),
  };
  const { phase } = view;
  // Held, the box stays above the card's halo, as the composer raises its own.
  const raised = phase === 'listening' || phase === 'recording' || phase === 'armed' || phase === 'settling';
  const mouseOn = pointer === 'mouse' && (phase === 'pressing' || phase === 'listening' || phase === 'recording' || phase === 'armed');
  return (
    <div className="pointer-events-none absolute inset-0 z-50 flex flex-col justify-end">
      <ComposerSlot>
        <ComposerHoldCard hold={view} pointer={pointer} to={target} attachments={attachments} onEditFirst={noop} />
        <div className={clsx('absolute inset-0', raised && 'z-50')}>
          <ComposerHoldSurface phase={phase} pointer={pointer} progress={view.progress} point={point} />
          {/* The mouse is on the box while it holds, and outside it once cancel is armed. */}
          {mouseOn && <SystemPointer at={phase === 'armed' ? { x: point.x, y: -36 } : point} />}
        </div>
      </ComposerSlot>
    </div>
  );
}

function SystemPointer({ at }: { at: { x: number; y: number } }) {
  return (
    <MousePointer2
      className="absolute size-5 fill-foreground stroke-background"
      style={{ left: at.x, top: at.y }}
      aria-hidden="true"
    />
  );
}

/** Desktop, text in the box: a press is the system's again (caret, selection). The I-beam shows it. */
export function TextPressLayer() {
  return (
    <div className="pointer-events-none absolute inset-0 z-50 flex flex-col justify-end">
      <ComposerSlot>
        <TextCursor className="absolute left-1/4 top-6.5 size-5 -translate-y-1/2 text-foreground" aria-hidden="true" />
      </ComposerSlot>
    </div>
  );
}

/** A phone's edit menu over the composer, as the system draws it on a long press. */
export function SystemEditMenu({ items }: { items: string[] }) {
  return (
    <div className="pointer-events-none absolute inset-0 z-50 flex flex-col justify-end">
      <ComposerSlot>
        <span className="absolute bottom-full left-6 mb-1 flex items-center divide-x divide-background/25 overflow-hidden rounded-lg bg-foreground text-sm text-background shadow-xl">
          {items.map((item) => (
            <span key={item} className="px-3 py-2">{item}</span>
          ))}
        </span>
      </ComposerSlot>
    </div>
  );
}

const KEY_ROWS = [10, 9, 7];

/** A phone keyboard: it only shows that the keyboard is up. */
export function SoftKeyboard() {
  return (
    <div className="flex h-72 shrink-0 flex-col gap-2.5 border-t border-border bg-surface-3 px-1 pt-2 pb-6" aria-hidden="true">
      <div className="flex h-8 items-center justify-around text-sm text-muted">
        <span>的</span>
        <span>我</span>
        <span>你</span>
      </div>
      {KEY_ROWS.map((count, row) => (
        <div key={row} className={clsx('flex justify-center gap-1.5', row === 1 && 'px-4', row === 2 && 'px-12')}>
          {Array.from({ length: count }, (_, index) => (
            <span key={index} className="h-10 flex-1 rounded-md bg-surface shadow-sm" />
          ))}
        </div>
      ))}
      <div className="flex gap-1.5">
        <span className="h-10 w-20 rounded-md bg-surface-2 shadow-sm" />
        <span className="h-10 flex-1 rounded-md bg-surface shadow-sm" />
        <span className="h-10 w-20 rounded-md bg-surface-2 shadow-sm" />
      </div>
    </div>
  );
}
