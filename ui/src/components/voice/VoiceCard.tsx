import { useLayoutEffect, useRef, type ReactNode } from 'react';
import { AlertTriangle, ChevronUp, CloudOff, MessagesSquare, MicOff, RotateCcw, Send, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import type { BotAvatar as BotAvatarSpec } from '@/lib/bots';
import type { VoiceLevelSource } from '@/lib/voiceLevel';
import type { VoiceCaptureState } from '@/lib/voiceRecording';
import { BotAvatar } from '../ui/bot-avatar';
import { Button } from '../ui/button';
import type { HoldInterruptReason } from './useHoldToTalk';
import { VoiceMeter } from './VoiceMeter';

// The parts every hold-to-talk shares (ui/design: mobile/main-bot-voice/d-recording,
// composer/hold-mobile/d-recording): the card the take is heard in, and the target the
// finger slides to to cancel. The tab bar's main Bot anchors them above the bar; a
// composer anchors them above itself.

export type VoiceCardPhase = 'listening' | 'recording' | 'armed' | 'settling' | 'interrupted';

/** Who the take goes to. `title` replaces "To {name}" (a Session's 发到「…」); with no avatar
 *  the card shows a neutral conversation icon. */
export type VoiceTarget = { name: string; avatar?: BotAvatarSpec | null; title?: string };

/** A microphone that gives the take no sound: its meter flat. */
const NO_SOUND_LEVEL: VoiceLevelSource = { read: () => 0, still: Array.from({ length: 24 }, () => 0) };

/** A take recorded without Web Audio has no level at all, so it shows no meter: a dot that breathes
 *  (opacity only; still with reduced motion) beside a still line, the clock above counting. */
function VoiceRecordingMark() {
  return (
    <div className="flex h-6 items-center gap-2" aria-hidden="true" data-voice-recording>
      <span className="size-2 rounded-full bg-mint motion-safe:animate-pulse" />
      <span className="h-px w-32 rounded-full bg-mint/30" />
    </div>
  );
}

const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

/** The take as it is heard: who it goes to, the clock, the live transcript (the settled part dark,
 *  the tail the recognizer may still revise light), and the meter (the microphone's real level;
 *  flat and red while a release would cancel; a sweep beside "Finishing…", with a gray × that stops
 *  waiting), or — once the take was cut off — why, and what can be done (`actions`, default Retry /
 *  Send). A long transcript
 *  scrolls inside the card and stays on its newest line while the take runs, with the top edge
 *  fading to say there is more above. */
export function VoiceCard({
  to,
  phase,
  text = '',
  stash = '',
  seconds,
  reason = 'dropped',
  long = false,
  actions,
  level,
  liveUnavailable = false,
  captureState = 'live',
  onCancel,
}: {
  to: VoiceTarget;
  phase: VoiceCardPhase;
  text?: string;
  stash?: string;
  seconds: number;
  reason?: HoldInterruptReason;
  /** Fade the top edge whatever the measured height (a static frame of a long take). */
  long?: boolean;
  actions?: ReactNode;
  /** The microphone's loudness, for the meter (`useHoldToTalk().level`). */
  level?: VoiceLevelSource | null;
  /** Live recognition is not working for this take: the card says so, quietly, and that the
   *  take is still transcribed in full on release (`useHoldToTalk().liveUnavailable`). */
  liveUnavailable?: boolean;
  /** Recorded without Web Audio (`recorder`: no level to draw, so the meter holds still), or so
   *  and getting no sound from the microphone (`no_sound`: the card says so; the take goes on). */
  captureState?: VoiceCaptureState;
  /** Settling: stop waiting for the take's words (`useHoldToTalk().cancelSettling`), the × at the
   *  end of the "Finishing…" line (ui/design: composer/hold-mobile/f-settling). */
  onCancel?: (() => void) | null;
}) {
  const { t } = useTranslation();
  const scroller = useRef<HTMLDivElement | null>(null);
  const fade = useRef<HTMLDivElement | null>(null);
  const armed = phase === 'armed';
  const interrupted = phase === 'interrupted';
  const running = phase === 'listening' || phase === 'recording' || armed;
  // Keep the newest words in view while they arrive (once the take is over the reader scrolls),
  // and fade the top edge only when there is more above.
  useLayoutEffect(() => {
    const node = scroller.current;
    if (!node) return;
    if (running) node.scrollTop = node.scrollHeight;
    if (fade.current) fade.current.hidden = !long && node.scrollHeight <= node.clientHeight + 1;
  }, [long, running, text, stash]);
  // What the card says about a cut-off take never points at words that are not there.
  const heard = Boolean(`${text}${stash}`.trim());
  const reasonCopy: Record<HoldInterruptReason, string> = heard
    ? {
        dropped: t('voice.interrupted.dropped'),
        failed: t('voice.interrupted.failed'),
        system: t('voice.interrupted.system'),
        'send-failed': t('voice.interrupted.sendFailed'),
      }
    : {
        dropped: t('voice.interrupted.droppedEmpty'),
        failed: t('voice.interrupted.failedEmpty'),
        system: t('voice.interrupted.systemEmpty'),
        'send-failed': t('voice.interrupted.sendFailed'),
      };
  return (
    // The page right around the card is dimmed and blurred, strongest at its edge and clear a short
    // way out (`.voice-card-halo`), so the rest of the screen stays readable while the user talks
    // (owner decision, 2026-11-04). Every hold-to-talk draws this card, so they all share it.
    <div className="relative w-full">
      <div className="voice-card-halo" aria-hidden="true" />
      <div
        className={clsx(
          'flex w-full flex-col gap-3 rounded-3xl border bg-surface p-4 shadow-elevation-2 transition',
          armed && 'border-destructive/40 opacity-60',
          interrupted && 'border-gold/45',
          !armed && !interrupted && 'border-mint/35 shadow-glow-md-mint',
        )}
      >
        <div className="flex items-center gap-2 text-xs">
          {to.avatar !== undefined ? (
            <BotAvatar avatar={to.avatar} name={to.name} size={22} />
          ) : (
            <span className="grid size-5.5 place-items-center rounded-full bg-surface-2 text-muted">
              <MessagesSquare className="size-3" />
            </span>
          )}
          <span className="min-w-0 truncate font-semibold text-foreground">{to.title ?? t('voice.to', { name: to.name })}</span>
          <span className="flex-1" />
          {interrupted ? (
            <span className="rounded-full bg-gold-soft px-2 py-0.5 font-semibold text-gold-ink">{t('voice.cutOff')}</span>
          ) : (
            <span className={clsx('font-mono', armed ? 'text-destructive-ink' : 'text-muted')}>{clock(seconds)}</span>
          )}
        </div>
        <div className="relative">
          <div ref={scroller} className={clsx('max-h-40', running ? 'overflow-hidden' : 'overflow-y-auto')} aria-live="polite">
            <p className={clsx('min-h-14 text-lg leading-relaxed', armed && 'line-through decoration-destructive/60')}>
              {text || stash ? (
                <>
                  <span className="text-foreground">{text}</span>
                  <span className="text-muted">{stash}</span>
                </>
              ) : (
                <span className="text-muted">{running ? t('pet.voice.listening') : t('voice.noText')}</span>
              )}
            </p>
          </div>
          <div ref={fade} hidden className="pointer-events-none absolute inset-x-0 top-0 h-10 bg-linear-to-b from-surface to-transparent" aria-hidden="true" />
        </div>
        {phase === 'settling' && (
          <div className="flex items-center gap-3 text-xs text-mint-ink">
            <VoiceMeter level={level} mode="settling" />
            {t('pet.voice.finishing')}
            {onCancel && (
              <>
                <span className="flex-1" />
                <VoiceClose label={t('voice.cancelSettling')} onClick={onCancel} />
              </>
            )}
          </div>
        )}
        {interrupted && (
          <div className="flex flex-col gap-3">
            <div className="flex items-start gap-2 text-xs leading-relaxed text-foreground">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-gold-ink" />
              {reasonCopy[reason]}
            </div>
            {actions && <div className="flex justify-end gap-2">{actions}</div>}
          </div>
        )}
        {running && !armed && captureState === 'no_sound' && (
          <div className="flex items-center gap-1.5 text-xs text-gold-ink" role="status">
            <MicOff className="size-3.5 shrink-0" />
            {t('voice.noSoundNow')}
          </div>
        )}
        {running && !armed && captureState !== 'no_sound' && liveUnavailable && (
          <div className="flex items-center gap-1.5 text-xs text-muted" role="status">
            <CloudOff className="size-3.5 shrink-0" />
            {t('voice.liveUnavailable')}
          </div>
        )}
        {running && (captureState === 'recorder' && !armed
          ? <VoiceRecordingMark />
          : <VoiceMeter level={captureState === 'no_sound' ? NO_SOUND_LEVEL : level} mode={armed ? 'armed' : 'live'} />)}
      </div>
    </div>
  );
}

/** The card's default actions for an interrupted take: discard, retry (when the recording can be
 *  transcribed again), send what was heard. */
export function VoiceInterruptedActions({
  canRetry,
  canSend,
  onDiscard,
  onRetry,
  onSend,
}: {
  canRetry: boolean;
  canSend: boolean;
  onDiscard: () => void;
  onRetry: () => void;
  onSend: () => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      <Button type="button" variant="ghost" size="sm" className="mr-auto" onClick={onDiscard}>
        {t('voice.discard')}
      </Button>
      {canRetry && (
        <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={onRetry}>
          <RotateCcw className="size-3.5" />
          {t('voice.retry')}
        </Button>
      )}
      <Button type="button" variant="brand" size="sm" className="gap-1.5" disabled={!canSend} onClick={onSend}>
        <Send className="size-3.5" />
        {t('voice.send')}
      </Button>
    </>
  );
}

/** Whatever else a hold lays over the page (the cancel target and its hint, a mouse's hint, the
 *  attachments note), in the card's halo made small (`.voice-overlay-halo`, owner request
 *  2026-11-05): the page under it dims and blurs, so overlay text never sits on the page's own
 *  text. The halo is drawn behind `children`, never over their own background. */
export function VoiceHalo({ children, className, shape }: { children: ReactNode; className?: string; shape?: 'cancel' }) {
  return (
    <span className={clsx('relative isolate flex max-w-full', className)}>
      <span className={clsx('voice-overlay-halo', shape === 'cancel' && 'voice-cancel-halo')} aria-hidden="true" />
      {children}
    </span>
  );
}

/** Where the finger goes to cancel, and the hint. Armed, it fills red and says "Release to cancel".
 *  One halo for both, shaped to them (`.voice-cancel-halo`), so neither part's halo blurs the other. */
export function VoiceCancelTarget({ armed, hint }: { armed: boolean; hint?: ReactNode }) {
  const { t } = useTranslation();
  return (
    <VoiceHalo shape="cancel">
      <div className="flex flex-col items-center gap-2">
        <span
          className={clsx(
            'grid size-14 place-items-center rounded-full border transition',
            armed ? 'scale-110 border-destructive bg-destructive text-primary-foreground shadow-elevation-2' : 'border-edge bg-field text-muted',
          )}
        >
          <X className="size-6" />
        </span>
        <span className={clsx('flex items-center gap-1 text-xs font-semibold', armed ? 'text-destructive-ink' : 'text-muted')}>
          {armed ? t('voice.releaseToCancel') : hint ?? (
            <>
              <ChevronUp className="size-3.5" />
              {t('voice.slideToCancel')}
            </>
          )}
        </span>
      </div>
    </VoiceHalo>
  );
}

/** The small gray × on a voice toast (close) or on a card that is finishing (stop waiting): its
 *  target is a fingertip (44 pt) without making its line taller, overlapping the padding at the
 *  right edge, so nothing moves when it comes or goes. */
export function VoiceClose({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className="-my-3 -mr-3.5 grid size-11 shrink-0 place-items-center rounded-2xl text-muted hover:text-foreground"
    >
      <X className="size-4" />
    </button>
  );
}
