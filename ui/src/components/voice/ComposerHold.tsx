import { useEffect } from 'react';
import { CloudOff, Loader2, Mic, MicOff, Paperclip, PenLine, RotateCcw, Send, Trash2, Undo2, X } from 'lucide-react';
import clsx from 'clsx';
import { Trans, useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import type { HoldOutcome, useHoldToTalk } from './useHoldToTalk';
import { VoiceCancelTarget, VoiceCard, VoiceToastClose, type VoiceTarget } from './VoiceCard';

// Holding an empty composer to talk (ui/design: composer/hold-desktop/*, composer/hold-mobile/*):
// the take's card above the composer, what the composer itself shows while it is held, and the
// note a press that sent nothing leaves behind. The hold itself is `useHoldToTalk`; the composer
// decides when a press may become one.

/** What these parts draw from a hold. */
export type ComposerHoldView = Pick<
  ReturnType<typeof useHoldToTalk>,
  'phase' | 'interruptReason' | 'text' | 'stash' | 'seconds' | 'progress' | 'outcome' | 'canRetry' | 'sendInterrupted' | 'retry' | 'discard' | 'dismiss'
>;

/** What pressed: a mouse (the ring, leave-to-cancel, Esc) or a finger or pen (slide up). */
export type HoldPointer = 'mouse' | 'touch';

const live = (phase: ComposerHoldView['phase']) => phase === 'listening' || phase === 'recording' || phase === 'armed';

// How long a note that needs nothing stays up.
const NOTE_MS = 3_000;

/** Above the composer: the take's card, what goes with it, and how to cancel; or the note a
 *  press left. Anchored to the composer's top edge. */
export function ComposerHoldCard({
  hold,
  pointer,
  to,
  attachments = [],
  onEditFirst,
}: {
  hold: ComposerHoldView;
  pointer: HoldPointer;
  to: VoiceTarget;
  /** The names of the files staged in the composer: they go with the spoken message. */
  attachments?: string[];
  /** An interrupted take's text, into the draft to edit before sending. */
  onEditFirst: () => void;
}) {
  const { t, i18n } = useTranslation();
  const { phase, outcome, dismiss } = hold;
  const quiet = outcome === 'cancelled' || outcome === 'too-short' || outcome === 'asking';
  useEffect(() => {
    if (!quiet) return;
    const timer = setTimeout(dismiss, NOTE_MS);
    return () => clearTimeout(timer);
  }, [quiet, dismiss]);
  const card = live(phase) || phase === 'settling' || phase === 'interrupted';
  const note = !card && outcome && outcome !== 'sent' ? outcome : null;
  if (!card && !note) return null;
  const heard = hold.text.trim().length > 0;
  const mouse = pointer === 'mouse';
  return (
    <div
      className={clsx('absolute inset-x-0 bottom-full z-50 mb-3 flex flex-col items-center', mouse ? 'gap-3' : 'gap-5')}
      role={card ? 'dialog' : undefined}
      aria-label={card ? to.title ?? t('voice.to', { name: to.name }) : undefined}
    >
      {card && (
        <div className={clsx('flex w-full flex-col items-center', mouse && 'max-w-xl')}>
          <VoiceCard
            to={to}
            phase={phase as Exclude<ComposerHoldView['phase'], 'idle' | 'pressing'>}
            text={hold.text}
            stash={hold.stash}
            seconds={hold.seconds}
            reason={hold.interruptReason ?? 'dropped'}
            actions={
              <>
                <Button type="button" variant="ghost" size="icon" className="mr-auto size-8 text-muted" aria-label={t('voice.discard')} onClick={hold.discard}>
                  <Trash2 className="size-4" />
                </Button>
                <Button type="button" variant="ghost" size="sm" className="gap-1.5" disabled={!heard} onClick={onEditFirst}>
                  <PenLine className="size-3.5" />
                  {t('voice.composer.editFirst')}
                </Button>
                {hold.canRetry && (
                  <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={hold.retry}>
                    <RotateCcw className="size-3.5" />
                    {t('voice.retry')}
                  </Button>
                )}
                <Button type="button" variant="brand" size="sm" className="gap-1.5" disabled={!heard} onClick={hold.sendInterrupted}>
                  <Send className="size-3.5" />
                  {t('voice.send')}
                </Button>
              </>
            }
          />
        </div>
      )}
      {card && attachments.length > 0 && (
        <span className="flex max-w-full items-center gap-1.5 rounded-full border border-border-strong bg-surface px-3 py-1 text-xs text-muted shadow-md">
          <Paperclip className="size-3.5 shrink-0" />
          <span className="min-w-0 truncate">
            <Trans
              i18nKey="voice.composer.withAttachments"
              values={{ names: new Intl.ListFormat(i18n.language, { style: 'short', type: 'conjunction' }).format(attachments) }}
              components={{ names: <span className="font-medium text-foreground" /> }}
            />
          </span>
        </span>
      )}
      {live(phase) && (mouse ? <MouseHint armed={phase === 'armed'} /> : <VoiceCancelTarget armed={phase === 'armed'} />)}
      {note && <HoldNote note={note} pointer={pointer} onDismiss={dismiss} />}
    </div>
  );
}

/** With a mouse there is nothing to slide to: release sends, leaving the box or Esc cancels.
 *  `box` is what is held, which the hint names: the composer, or a Bot's avatar. */
export function MouseHint({ armed, box = 'composer' }: { armed: boolean; box?: 'composer' | 'avatar' }) {
  const { t } = useTranslation();
  return (
    <span
      className={clsx(
        'flex items-center gap-1.5 rounded-full border bg-surface px-3 py-1 text-xs font-semibold shadow-md',
        armed ? 'border-destructive/50 text-destructive-ink' : 'border-border-strong text-muted',
      )}
    >
      {armed ? (
        t(box === 'avatar' ? 'voice.avatar.mouseArmed' : 'voice.composer.mouseArmed')
      ) : (
        <Trans
          i18nKey={box === 'avatar' ? 'voice.avatar.mouseHint' : 'voice.composer.mouseHint'}
          components={{ key: <kbd className="rounded border border-border-strong bg-surface-2 px-1 font-mono text-2xs text-foreground" /> }}
        />
      )}
    </span>
  );
}

/** What a press that sent nothing leaves: one line where the card was; typing is always the way on. */
function HoldNote({ note, pointer, onDismiss }: { note: Exclude<HoldOutcome, 'sent'>; pointer: HoldPointer; onDismiss: () => void }) {
  const { t } = useTranslation();
  return (
    <div
      role="status"
      className={clsx(
        'flex w-full max-w-sm items-start gap-3 rounded-2xl border bg-surface-2 px-3.5 py-3 shadow-xl',
        note === 'denied' ? 'border-destructive/35' : note === 'asking' ? 'border-mint/40 shadow-glow-md-mint' : 'border-border-strong',
      )}
    >
      {note === 'asking' && <Mic className="mt-0.5 size-4 shrink-0 text-mint-ink" />}
      {note === 'cancelled' && <Undo2 className="mt-0.5 size-4 shrink-0 text-muted" />}
      {note === 'too-short' && <MicOff className="mt-0.5 size-4 shrink-0 text-muted" />}
      {note === 'denied' && <MicOff className="mt-0.5 size-4 shrink-0 text-destructive-ink" />}
      {note === 'unavailable' && <CloudOff className="mt-0.5 size-4 shrink-0 text-muted" />}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-xs leading-relaxed">
        {note === 'cancelled' && <span className="text-foreground">{t('voice.cancelled')}</span>}
        {note === 'too-short' && <span className="text-foreground">{t('voice.composer.tooShort')}</span>}
        {note === 'asking' && <span className="text-foreground">{t('voice.asking')}</span>}
        {note === 'denied' && (
          <>
            <span className="font-semibold text-foreground">{t('voice.deniedTitle')}</span>
            <span className="text-muted">{t(pointer === 'mouse' ? 'voice.composer.deniedBrowser' : 'voice.composer.deniedSystem')}</span>
          </>
        )}
        {note === 'unavailable' && <span className="text-foreground">{t('pet.voice.cloudRequired')}</span>}
      </div>
      <VoiceToastClose label={t('voice.dismiss')} onClick={onDismiss} />
    </div>
  );
}

/** In the composer's box while it is held. Pressing: a mouse sees a ring fill at the pointer
 *  (from the gesture's ring delay); a finger sees the box light up. Then the whole box is the
 *  button the finger is on, "Release to send", red once a release would cancel; settling, it
 *  waits for the message to go. */
export function ComposerHoldSurface({
  phase,
  pointer,
  progress,
  point,
}: {
  phase: ComposerHoldView['phase'];
  pointer: HoldPointer;
  progress: number;
  /** Where the press is, in the box's own coordinates (the ring sits there). */
  point: { x: number; y: number } | null;
}) {
  const { t } = useTranslation();
  if (phase === 'idle' || phase === 'interrupted') return null;
  if (phase === 'pressing') {
    if (pointer === 'touch') return <div className="pointer-events-none absolute inset-0 rounded-2xl bg-mint/10 ring-2 ring-mint/45" aria-hidden="true" />;
    if (progress <= 0) return null;
    return (
      <div className="pointer-events-none absolute inset-0 rounded-2xl ring-2 ring-mint/45" aria-hidden="true">
        <span
          className="absolute flex -translate-x-1/2 -translate-y-1/2 flex-col items-center"
          style={{ left: point?.x ?? '50%', top: point?.y ?? '50%' }}
        >
          <span className="absolute bottom-full mb-4 whitespace-nowrap rounded-full border border-border-strong bg-surface px-2.5 py-1 text-xs font-semibold text-foreground shadow-md">
            {t('voice.composer.keepHolding')}
          </span>
          <PressRing progress={progress} />
        </span>
      </div>
    );
  }
  if (phase === 'settling') {
    return (
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-2xl border border-border-strong bg-surface-2 text-muted" aria-hidden="true">
        <Loader2 className="size-4 animate-spin" />
      </div>
    );
  }
  const armed = phase === 'armed';
  return (
    <div
      className={clsx(
        'pointer-events-none absolute inset-0 flex items-center justify-center gap-2 rounded-2xl border-2 bg-surface-2 text-sm font-semibold transition',
        armed ? 'border-destructive/70 text-destructive-ink' : 'border-mint text-mint-ink shadow-glow-md-mint',
      )}
      aria-hidden="true"
    >
      {armed ? <X className="size-4" /> : <Mic className="size-4" />}
      {armed ? t('voice.releaseToCancel') : t('voice.composer.releaseToSend')}
    </div>
  );
}

/** The ring a mouse press fills, from the gesture's ring delay to its hold. */
export function PressRing({ progress }: { progress: number }) {
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
          strokeDasharray={`${circumference * Math.min(1, Math.max(0, progress))} ${circumference}`}
        />
      </svg>
      <Mic className="size-4 text-mint-ink" />
    </span>
  );
}
