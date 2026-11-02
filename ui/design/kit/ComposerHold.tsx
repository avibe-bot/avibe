// Hold the empty composer to talk (owner direction, 2026-11-02), on every composer and on
// desktop and phone alike, the way WeChat merges its input with its hold-to-talk button.
// While held, the shipped dictation pipeline streams the transcript into the same card the
// center of the tab bar uses (MainBotVoice); releasing sends it to this conversation.
//
// A press records only while the composer is empty (on a phone, also with the keyboard
// down); otherwise the press belongs to the system (caret, selection, Paste). A desktop
// press shows a ring at the pointer that fills before recording starts, so a slow click
// never opens the microphone. Cancel: slide up on touch; leave the composer or press Esc
// with a mouse.
//
// Drawn over the shipped chat page: this layer covers the page above the composer and lays
// its own surface exactly over the shipped composer (an invisible shipped Composer sizes it).
import { CloudOff, Loader2, MessageSquare, Mic, MicOff, MousePointer2, PenLine, RotateCcw, Send, TextCursor, Trash2, X } from 'lucide-react';
import clsx from 'clsx';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { BotAvatar } from '@/components/ui/bot-avatar';
import { Composer } from '@/components/workbench/Composer';
import { useBots } from '@/context/BotsContext';

import { useDraft } from './draft';
import { VoiceCancelTarget, VoiceCard } from './MainBotVoice';

export type HoldDevice = 'desktop' | 'phone';

/** Where a hold is: pressing (desktop: the ring is filling; phone: before 300 ms), listening (the
 *  microphone is open, no words yet), recording, armed (released now, it cancels), settling (released,
 *  the final text is on its way), or interrupted (the take ended without a release; its text is kept). */
export type HoldPhase = 'pressing' | 'listening' | 'recording' | 'armed' | 'settling' | 'interrupted';

/** One line in the card's place after a hold, or while the system asks for the microphone. */
export type HoldNote = 'asking' | 'cancelled' | 'too-short' | 'denied' | 'unavailable';

/** Who the composer talks to: a Bot by handle, or a Session by its title. */
export type HoldTarget = { bot: string } | { session: string };

function useTarget(to: HoldTarget): { name: string; avatar?: ReactNode; title?: string } {
  const draft = useDraft();
  const { bots, displayName } = useBots();
  if ('session' in to) {
    return {
      name: to.session,
      title: draft(`发到「${to.session}」`, `To “${to.session}”`),
      avatar: (
        <span className="grid size-5.5 shrink-0 place-items-center rounded-full bg-surface-2 text-muted">
          <MessageSquare className="size-3" />
        </span>
      ),
    };
  }
  const bot = bots.find((each) => each.handle === to.bot);
  const name = bot ? displayName(bot) : to.bot;
  return { name, avatar: bot ? <BotAvatar avatar={bot.avatar} name={name} size={22} /> : undefined };
}

export function ComposerHoldLayer({
  device,
  to,
  phase,
  note,
  text,
  stash,
  seconds = 0,
  long = false,
  progress = 0.6,
}: {
  device: HoldDevice;
  to: HoldTarget;
  phase?: HoldPhase;
  note?: HoldNote;
  text?: string;
  stash?: string;
  seconds?: number;
  long?: boolean;
  /** How far the desktop ring has filled while pressing, 0 to 1. */
  progress?: number;
}) {
  const target = useTarget(to);
  const live = phase === 'listening' || phase === 'recording' || phase === 'armed';
  // The page dims while the microphone is open and while the final text settles.
  const dim = live || phase === 'settling';
  const showCard = live || phase === 'settling' || phase === 'interrupted';
  return (
    <div className="pointer-events-none absolute inset-0 z-50 flex flex-col">
      {/* The composer's own surface is drawn over the dim, so it stays bright under the finger. */}
      {dim && <div className="absolute inset-0 bg-background/80 backdrop-blur-sm" />}
      <div className="relative flex min-h-0 flex-1 flex-col items-center justify-end px-4 pb-4 md:px-8">
        <div className={clsx('pointer-events-auto relative flex w-full flex-col items-center', device === 'desktop' ? 'max-w-xl gap-3' : 'gap-5 px-1')}>
          {showCard && phase && (
            <VoiceCard
              to={target}
              phase={phase}
              text={text}
              stash={stash}
              seconds={seconds}
              long={long}
              actions={<InterruptedActions />}
            />
          )}
          {live && (device === 'phone' ? <VoiceCancelTarget armed={phase === 'armed'} /> : <DesktopHint armed={phase === 'armed'} />)}
          {note && <HoldNoteCard note={note} device={device} />}
        </div>
      </div>
      <ComposerSlot>{phase && <HoldSurface device={device} phase={phase} progress={progress} />}</ComposerSlot>
    </div>
  );
}

/** The composer's box, measured by an invisible shipped Composer placed where the chat page puts
 *  its own (Compose's padding), so a surface drawn in it lies exactly over the shipped one. */
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

/** What the composer itself shows during a hold: the ring at the pointer while pressing, then the
 *  whole box becomes the talk button ("松开发送"), red while a release would cancel. */
function HoldSurface({ device, phase, progress }: { device: HoldDevice; phase: HoldPhase; progress: number }) {
  const draft = useDraft();
  if (phase === 'interrupted') return null;
  if (phase === 'pressing') {
    return (
      <div className={clsx('pointer-events-auto absolute inset-0 rounded-2xl ring-2 ring-mint/45', device === 'phone' && 'bg-mint/10')}>
        {device === 'desktop' && (
          <span className="absolute left-1/3 top-6.5 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center">
            <span className="absolute bottom-full mb-4 whitespace-nowrap rounded-full border border-border-strong bg-surface px-2.5 py-1 text-xs font-semibold text-foreground shadow-md">
              {draft('继续按住，开始说话', 'Keep holding to talk')}
            </span>
            <PressRing progress={progress} />
            <SystemPointer className="left-1/2 top-1/2" />
          </span>
        )}
      </div>
    );
  }
  if (phase === 'settling') {
    // Locked until the message is in; the card above says what is happening.
    return (
      <div className="absolute inset-0 flex items-center justify-center rounded-2xl border border-border-strong bg-surface-2 text-muted">
        <Loader2 className="size-4 animate-spin" />
      </div>
    );
  }
  const armed = phase === 'armed';
  return (
    <div
      className={clsx(
        'pointer-events-auto absolute inset-0 flex items-center justify-center gap-2 rounded-2xl border-2 bg-surface-2 text-sm font-semibold transition',
        armed ? 'border-destructive/70 text-destructive-ink' : 'border-mint text-mint-ink shadow-glow-md-mint',
      )}
    >
      {armed ? <X className="size-4" /> : <Mic className="size-4" />}
      {armed ? draft('松开取消', 'Release to cancel') : draft('松开发送', 'Release to send')}
      {device === 'desktop' && (
        // The system pointer: on the box while it records, outside it once cancel is armed.
        <SystemPointer className={armed ? '-top-9 left-1/3' : 'left-1/3 top-6.5'} />
      )}
    </div>
  );
}

/** The desktop ring: it starts after a short pause and fills before the microphone opens. */
function PressRing({ progress }: { progress: number }) {
  const circumference = 2 * Math.PI * 15;
  return (
    <span className="relative grid size-9 place-items-center rounded-full bg-surface shadow-glow-sm-mint">
      <svg viewBox="0 0 36 36" className="absolute inset-0 size-9 -rotate-90" aria-hidden="true">
        <circle cx="18" cy="18" r="15" fill="none" strokeWidth="3" className="stroke-border-strong" />
        <circle
          cx="18"
          cy="18"
          r="15"
          fill="none"
          strokeWidth="3"
          strokeLinecap="round"
          className="stroke-mint"
          strokeDasharray={`${circumference * progress} ${circumference}`}
        />
      </svg>
      <Mic className="size-4 text-mint-ink" />
    </span>
  );
}

/** The operating system's arrow, drawn so a still frame shows where the mouse is. Not product UI. */
function SystemPointer({ className }: { className: string }) {
  return <MousePointer2 className={clsx('absolute size-5 fill-foreground stroke-background', className)} aria-hidden="true" />;
}

/** The desktop counterpart of the touch cancel target: there is nothing to slide to. */
function DesktopHint({ armed }: { armed: boolean }) {
  const draft = useDraft();
  return (
    <span
      className={clsx(
        'flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold shadow-md',
        armed ? 'border-destructive/50 bg-surface text-destructive-ink' : 'border-border-strong bg-surface text-muted',
      )}
    >
      {armed ? (
        draft('松开取消 · 移回输入框继续说', 'Release to cancel · move back to keep talking')
      ) : (
        <>
          {draft('松开发送 · 移出输入框或按', 'Release to send · move off the box or press')}
          <kbd className="rounded border border-border-strong bg-surface-2 px-1 font-mono text-2xs text-foreground">Esc</kbd>
          {draft('取消', 'to cancel')}
        </>
      )}
    </span>
  );
}

/** An interrupted take keeps what was heard: send it, transcribe the kept recording again, or put it
 *  in the composer to edit. The bin discards it. */
function InterruptedActions() {
  const draft = useDraft();
  return (
    <div className="flex flex-col gap-3">
      <span className="text-xs leading-relaxed text-muted">
        {draft('网络断开了。说过的话都在，录音也留着。', 'The connection dropped. What you said is kept, and so is the recording.')}
      </span>
      <div className="flex items-center gap-2">
        <Button type="button" size="sm">
          <Send className="size-3.5" />
          {draft('发送', 'Send')}
        </Button>
        <Button type="button" size="sm" variant="secondary">
          <RotateCcw className="size-3.5" />
          {draft('重试', 'Retry')}
        </Button>
        <Button type="button" size="sm" variant="ghost">
          <PenLine className="size-3.5" />
          {draft('放进输入框', 'Edit first')}
        </Button>
        <span className="flex-1" />
        <Button type="button" size="icon" variant="ghost" aria-label={draft('丢弃', 'Discard')} className="size-8 text-muted">
          <Trash2 className="size-4" />
        </Button>
      </div>
    </div>
  );
}

/** What a hold led to when nothing was sent, or the system asking first: one card where the
 *  transcript was. Errors leave typing as the way on; the composer itself is unchanged. */
function HoldNoteCard({ note, device }: { note: HoldNote; device: HoldDevice }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const desktop = device === 'desktop';
  const tone = note === 'denied' ? 'border-destructive/35' : note === 'asking' ? 'border-mint/40 shadow-glow-md-mint' : 'border-border-strong';
  const sticky = note === 'denied' || note === 'unavailable';
  return (
    <div role="status" className={clsx('flex w-full max-w-sm items-start gap-3 rounded-2xl border bg-surface-2 px-3.5 py-3 shadow-xl', tone)}>
      {note === 'asking' && <Mic className="mt-0.5 size-4 shrink-0 text-mint-ink" />}
      {note === 'cancelled' && <X className="mt-0.5 size-4 shrink-0 text-muted" />}
      {note === 'too-short' && <MicOff className="mt-0.5 size-4 shrink-0 text-muted" />}
      {note === 'denied' && <MicOff className="mt-0.5 size-4 shrink-0 text-destructive-ink" />}
      {note === 'unavailable' && <CloudOff className="mt-0.5 size-4 shrink-0 text-muted" />}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-xs leading-relaxed">
        {note === 'asking' && (
          <>
            <span className="font-semibold text-foreground">{draft('请允许使用麦克风', 'Allow the microphone')}</span>
            <span className="text-muted">
              {desktop
                ? draft('浏览器正在询问。允许后，再按住输入框说话。', 'Your browser is asking. Once allowed, hold the box again to talk.')
                : draft('系统正在询问。允许后，再按住输入框说话。', 'Your phone is asking. Once allowed, hold the box again to talk.')}
            </span>
          </>
        )}
        {note === 'cancelled' && <span className="text-foreground">{draft('已取消，没有发送。', 'Cancelled. Nothing was sent.')}</span>}
        {note === 'too-short' && <span className="text-foreground">{draft('没听到说话，没有发送。按住再说一次。', 'Nothing heard, so nothing was sent. Hold to try again.')}</span>}
        {note === 'denied' && (
          <>
            <span className="font-semibold text-foreground">{draft('无法使用麦克风', 'The microphone is off')}</span>
            <span className="text-muted">
              {desktop
                ? draft('在浏览器的网站设置里允许使用麦克风，或直接打字。', "Allow the microphone in the browser's site settings, or type instead.")
                : draft('在系统设置里允许使用麦克风，或直接打字。', 'Allow the microphone in system settings, or type instead.')}
            </span>
          </>
        )}
        {note === 'unavailable' && (
          <>
            <span className="text-foreground">{t('chat.compose.voiceUnavailable')}</span>
            <span className="text-muted">{draft('可以直接打字发送。', 'You can type instead.')}</span>
          </>
        )}
      </div>
      {sticky && <X className="mt-0.5 size-3.5 shrink-0 text-muted" aria-label={draft('关闭', 'Dismiss')} />}
    </div>
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

/** A phone's edit menu over the composer, as the system draws it on a long press. A stand-in for
 *  system UI, not product UI. */
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

/** A phone keyboard, as a stand-in for system UI: it only shows that the keyboard is up. */
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
