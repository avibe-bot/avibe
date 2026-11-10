import { useEffect } from 'react';
import { ArrowUpRight, CloudOff, MicOff, RotateCcw, Undo2 } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import type { BotAvatar as BotAvatarSpec } from '@/lib/bots';
import { isDesktopShell } from '@/lib/desktopShell';
import { isApplePlatform } from '@/lib/platform';
import type { VoiceLevelSource } from '@/lib/voiceLevel';
import type { VoiceCaptureState } from '@/lib/voiceRecording';
import { BotAvatar } from '../ui/bot-avatar';
import type { useHoldToTalk } from './useHoldToTalk';
import { VoiceCancelTarget, VoiceCard, VoiceInterruptedActions, VoiceClose } from './VoiceCard';
import { VoiceMark } from './VoiceMark';

// Holding the main Bot in the middle of the phone's tab bar (ui/design: mobile/main-bot-voice/*):
// the take's card above the bar with the cancel target under it, and afterwards one toast
// above the bar that says what happened. The hold itself is `useHoldToTalk`.

/** What the overlay and the toast draw from a hold: its state and the choices it offers. */
export type HoldView = Pick<
  ReturnType<typeof useHoldToTalk>,
  'phase' | 'interruptReason' | 'text' | 'stash' | 'seconds' | 'outcome' | 'canRetry' | 'sendInterrupted' | 'retry' | 'discard' | 'dismiss'
> & {
  /** Stops waiting for a finishing take's words (the card's ×); absent or null when it cannot. */
  cancelSettling?: (() => void) | null;
  /** The microphone's loudness for the card's meter. */
  level?: VoiceLevelSource | null;
  /** Live recognition is not working for this take. */
  liveUnavailable?: boolean;
  /** How the take is recorded: through Web Audio, without it, or without it and getting no sound. */
  captureState?: VoiceCaptureState;
};
type Hold = HoldView;
type MainBot = { name: string; avatar: BotAvatarSpec | null };

/** A take with nothing heard that could not be transcribed: the design's toast, not the card. */
const failedQuietly = (hold: Hold) => hold.phase === 'interrupted' && hold.interruptReason === 'failed' && !hold.text.trim();

/** The card while the take runs, settles, or waits after being cut off. */
export function MainBotHoldOverlay({ hold, bot }: { hold: Hold; bot: MainBot }) {
  const { t } = useTranslation();
  const { phase } = hold;
  if (phase === 'idle' || phase === 'pressing' || failedQuietly(hold)) return null;
  const holding = phase === 'listening' || phase === 'recording' || phase === 'armed';
  return (
    <div className="md:hidden" role="dialog" aria-label={t('voice.to', { name: bot.name })}>
      <div className="fixed inset-x-0 bottom-(--mobile-nav-height) z-50 flex flex-col items-center gap-5 px-5 pb-10">
        <VoiceCard
          to={{ name: bot.name, avatar: bot.avatar }}
          phase={phase}
          text={hold.text}
          stash={hold.stash}
          seconds={hold.seconds}
          reason={hold.interruptReason ?? 'dropped'}
          level={hold.level}
          liveUnavailable={hold.liveUnavailable}
          captureState={hold.captureState}
          onCancel={hold.cancelSettling}
          actions={
            <VoiceInterruptedActions
              canRetry={hold.canRetry}
              canSend={Boolean(hold.text.trim())}
              onDiscard={hold.discard}
              onRetry={hold.retry}
              onSend={hold.sendInterrupted}
            />
          }
        />
        {holding && <VoiceCancelTarget armed={phase === 'armed'} />}
      </div>
    </div>
  );
}

// How long an outcome stays up. A failure that can be retried waits for the reader.
const TOAST_MS = 4_000;

/** What the last press led to, as one toast above the bar. Errors leave typing as the way on:
 *  a tap on the main Bot opens its conversation. `bar`: the phone tab bar's center, which only a
 *  phone has. `shell`: a Bot's avatar anywhere, so a desktop shows it too, where the app's toasts
 *  go (bottom right). */
export function MainBotVoiceToast({ hold, bot, onView, placement = 'bar' }: { hold: Hold; bot: MainBot; onView: () => void; placement?: 'bar' | 'shell' }) {
  const { t } = useTranslation();
  const { outcome, dismiss } = hold;
  const failed = failedQuietly(hold);
  useEffect(() => {
    if (!outcome) return;
    const timer = setTimeout(dismiss, TOAST_MS);
    return () => clearTimeout(timer);
  }, [outcome, dismiss]);
  if (!outcome && !failed) return null;
  const tone = outcome === 'denied' || failed ? 'border-destructive/35' : outcome === 'sent' ? 'border-mint/40 shadow-glow-md-mint' : 'border-edge';
  return (
    <div
      className={clsx(
        'pointer-events-none fixed inset-x-0 bottom-(--mobile-nav-height) z-50 flex justify-center px-4 pb-9',
        placement === 'bar' ? 'md:hidden' : 'md:inset-x-auto md:right-4 md:bottom-4 md:w-sm md:px-0 md:pb-0',
      )}
      role="status"
    >
      <div className={clsx('pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-2xl border bg-surface-2 px-3.5 py-3 shadow-elevation-2', tone)}>
        {outcome === 'sent' && <BotAvatar avatar={bot.avatar} name={bot.name} size={28} />}
        {(outcome === 'cancelled' || outcome === 'kept') && <Undo2 className="mt-0.5 size-4 shrink-0 text-muted" />}
        {(outcome === 'too-short' || outcome === 'asking') && <MicOff className="mt-0.5 size-4 shrink-0 text-muted" />}
        {(outcome === 'no-sound' || outcome === 'mic-busy' || outcome === 'no-device' || outcome === 'mic-failed') && <MicOff className="mt-0.5 size-4 shrink-0 text-gold-ink" />}
        {(outcome === 'denied' || failed) && <MicOff className="mt-0.5 size-4 shrink-0 text-destructive-ink" />}
        {outcome === 'unavailable' && <CloudOff className="mt-0.5 size-4 shrink-0 text-muted" />}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-xs leading-relaxed">
          {failed ? (
            <>
              <span className="font-semibold text-foreground">{t('pet.voice.failed')}</span>
              <span className="text-muted">{t('voice.failedBody')}</span>
            </>
          ) : (
            <>
              {outcome === 'sent' && (
                <>
                  <span className="flex items-center gap-1.5 font-semibold text-foreground">
                    {t('voice.sent', { name: bot.name })}
                    <VoiceMark />
                  </span>
                  <span className="line-clamp-1 text-muted">{hold.text}</span>
                </>
              )}
              {outcome === 'cancelled' && <span className="text-foreground">{t('voice.cancelled')}</span>}
              {outcome === 'kept' && <span className="text-foreground">{t('voice.cancelledKept', { name: bot.name })}</span>}
              {outcome === 'too-short' && <span className="text-foreground">{t('voice.tooShort')}</span>}
              {outcome === 'no-sound' && <span className="text-foreground">{t('voice.noSound')}</span>}
              {outcome === 'mic-busy' && <span className="text-foreground">{t('voice.micBusy')}</span>}
              {outcome === 'no-device' && <span className="text-foreground">{t('voice.noDevice')}</span>}
              {outcome === 'mic-failed' && <span className="text-foreground">{t('voice.micFailed')}</span>}
              {outcome === 'asking' && <span className="text-foreground">{t('voice.asking')}</span>}
              {outcome === 'denied' && (
                <>
                  <span className="font-semibold text-foreground">{t('voice.deniedTitle')}</span>
                  <span className="text-muted">{t(isDesktopShell()
                    ? isApplePlatform() ? 'voice.deniedDesktop' : 'voice.deniedDesktopOther'
                    : 'voice.deniedBody', { name: bot.name })}</span>
                </>
              )}
              {outcome === 'unavailable' && (
                <>
                  <span className="text-foreground">{t('pet.voice.cloudRequired')}</span>
                  <span className="text-muted">{t('voice.tapToType', { name: bot.name })}</span>
                </>
              )}
            </>
          )}
        </div>
        {outcome === 'sent' && !failed && (
          <button type="button" onClick={() => { dismiss(); onView(); }} className="-my-3 flex min-h-11 shrink-0 items-center gap-0.5 self-center px-1.5 text-xs font-semibold text-mint-ink">
            {t('voice.view')}
            <ArrowUpRight className="size-3.5" />
          </button>
        )}
        {failed && hold.canRetry && (
          <button type="button" onClick={hold.retry} className="-my-3 flex min-h-11 shrink-0 items-center gap-1 self-center px-1.5 text-xs font-semibold text-mint-ink">
            <RotateCcw className="size-3.5" />
            {t('voice.retry')}
          </button>
        )}
        {failed ? <VoiceClose label={t('voice.discard')} onClick={hold.discard} /> : <VoiceClose label={t('voice.dismiss')} onClick={dismiss} />}
      </div>
    </div>
  );
}
