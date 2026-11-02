// Press and hold the main Bot to speak to it (owner direction, 2026-11-02). While
// held, the shipped dictation pipeline (VoiceRealtimeSession) streams a live
// transcript into the card; sliding up into the cancel target arms cancel;
// releasing sends the final text straight to the main Bot's conversation, or
// cancels. Afterwards one toast above the bar says what happened.
import { ArrowUpRight, ChevronUp, CloudOff, Loader2, MicOff, RotateCcw, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { BotAvatar } from '@/components/ui/bot-avatar';
import { useBots } from '@/context/BotsContext';

import { useDraft } from './draft';

export type VoicePhase = 'listening' | 'recording' | 'armed' | 'settling' | 'limit';

// A level meter's resting shape; the build drives it from the microphone level.
const LEVELS = ['h-1', 'h-1.5', 'h-2.5', 'h-2', 'h-3', 'h-2', 'h-3.5', 'h-3', 'h-1.5', 'h-2.5', 'h-3.5', 'h-2', 'h-1.5', 'h-2', 'h-3', 'h-1.5', 'h-1'];

const clock = (seconds: number) => `0:${String(seconds).padStart(2, '0')}`;

export function VoiceHoldOverlay({
  phase,
  text = '',
  stash = '',
  seconds,
}: {
  phase: VoicePhase;
  /** The stable part of the live transcript. */
  text?: string;
  /** The tail the recognizer may still revise, drawn lighter. */
  stash?: string;
  seconds: number;
}) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  if (!mainBot) return null;
  const name = displayName(mainBot);
  const armed = phase === 'armed';
  const settling = phase === 'settling';
  return (
    <div className="md:hidden" role="dialog" aria-modal="true" aria-label={draft(`说给${name}`, `Speak to ${name}`)}>
      <div className="fixed inset-x-0 top-0 bottom-(--mobile-nav-height) z-40 bg-background/80 backdrop-blur-sm" />
      <div className="fixed inset-x-0 bottom-(--mobile-nav-height) z-50 flex flex-col items-center gap-5 px-5 pb-10">
        <div
          className={clsx(
            'flex w-full flex-col gap-3 rounded-3xl border bg-surface p-4 shadow-xl transition',
            armed ? 'border-destructive/40 opacity-60' : 'border-mint/35 shadow-glow-md-mint',
          )}
        >
          <div className="flex items-center gap-2 text-xs">
            <BotAvatar avatar={mainBot.avatar} name={name} size={22} />
            <span className="font-semibold text-foreground">{draft(`说给${name}`, `To ${name}`)}</span>
            <span className="flex-1" />
            {phase === 'limit' ? (
              <span className="rounded-full bg-gold-soft px-2 py-0.5 font-semibold text-gold-ink">{draft(`还剩 ${60 - seconds} 秒`, `${60 - seconds}s left`)}</span>
            ) : (
              <span className={clsx('font-mono', armed ? 'text-destructive-ink' : 'text-muted')}>{clock(seconds)}</span>
            )}
          </div>
          <p className={clsx('min-h-14 text-lg leading-relaxed', armed && 'line-through decoration-destructive/60')}>
            {text || stash ? (
              <>
                <span className="text-foreground">{text}</span>
                <span className="text-muted">{stash}</span>
              </>
            ) : (
              <span className="text-muted">{t('pet.voice.listening')}</span>
            )}
          </p>
          {settling ? (
            <div className="flex items-center gap-2 text-xs text-mint-ink">
              <Loader2 className="size-3.5 animate-spin" />
              {t('pet.voice.finishing')}
            </div>
          ) : (
            <div className="flex h-4 items-center gap-1" aria-hidden="true">
              {LEVELS.map((level, index) => (
                <span key={index} className={clsx('w-1 rounded-full', level, armed ? 'bg-muted' : 'bg-mint')} />
              ))}
            </div>
          )}
        </div>
        {!settling && (
          <div className="flex flex-col items-center gap-2">
            <span
              className={clsx(
                'grid size-14 place-items-center rounded-full border transition',
                armed ? 'scale-110 border-destructive bg-destructive text-primary-foreground shadow-xl' : 'border-border-strong bg-surface text-muted',
              )}
            >
              <X className="size-6" />
            </span>
            <span className={clsx('flex items-center gap-1 text-xs font-semibold', armed ? 'text-destructive-ink' : 'text-muted')}>
              {armed ? draft('松开取消', 'Release to cancel') : (
                <>
                  <ChevronUp className="size-3.5" />
                  {draft('上滑取消 · 松开发送', 'Slide up to cancel · release to send')}
                </>
              )}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

export type VoiceOutcome = 'sent' | 'cancelled' | 'too-short' | 'denied' | 'unavailable' | 'failed';

/** What a released press led to, as one toast above the bar. Errors leave typing as the way on. */
export function VoiceToast({ outcome, text = '' }: { outcome: VoiceOutcome; text?: string }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  if (!mainBot) return null;
  const name = displayName(mainBot);
  const tone = outcome === 'denied' || outcome === 'failed' ? 'border-destructive/35' : outcome === 'sent' ? 'border-mint/40 shadow-glow-md-mint' : 'border-border-strong';
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-(--mobile-nav-height) z-50 flex justify-center px-4 pb-9 md:hidden" role="status">
      <div className={clsx('pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-2xl border bg-surface-2 px-3.5 py-3 shadow-xl', tone)}>
        {outcome === 'sent' && <BotAvatar avatar={mainBot.avatar} name={name} size={28} />}
        {outcome === 'cancelled' && <X className="mt-0.5 size-4 shrink-0 text-muted" />}
        {outcome === 'too-short' && <MicOff className="mt-0.5 size-4 shrink-0 text-muted" />}
        {outcome === 'denied' && <MicOff className="mt-0.5 size-4 shrink-0 text-destructive-ink" />}
        {outcome === 'unavailable' && <CloudOff className="mt-0.5 size-4 shrink-0 text-muted" />}
        {outcome === 'failed' && <MicOff className="mt-0.5 size-4 shrink-0 text-destructive-ink" />}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-xs leading-relaxed">
          {outcome === 'sent' && (
            <>
              <span className="font-semibold text-foreground">{draft(`已发给${name}`, `Sent to ${name}`)}</span>
              <span className="line-clamp-1 text-muted">{text}</span>
            </>
          )}
          {outcome === 'cancelled' && <span className="text-foreground">{draft('已取消，没有发送。', 'Cancelled. Nothing was sent.')}</span>}
          {outcome === 'too-short' && <span className="text-foreground">{draft('没听到说话。按住再说一次，或点一下打字。', 'Nothing heard. Hold to try again, or tap to type.')}</span>}
          {outcome === 'denied' && (
            <>
              <span className="font-semibold text-foreground">{draft('无法使用麦克风', 'The microphone is off')}</span>
              <span className="text-muted">{draft(`在系统设置里允许使用麦克风；也可以点一下${name}打字。`, `Allow the microphone in system settings, or tap ${name} to type.`)}</span>
            </>
          )}
          {outcome === 'unavailable' && (
            <>
              <span className="text-foreground">{t('pet.voice.cloudRequired')}</span>
              <span className="text-muted">{draft(`点一下${name}打字。`, `Tap ${name} to type.`)}</span>
            </>
          )}
          {outcome === 'failed' && (
            <>
              <span className="font-semibold text-foreground">{t('pet.voice.failed')}</span>
              <span className="text-muted">{draft('录音还在，可以重试转写。', 'The recording is kept; you can retry.')}</span>
            </>
          )}
        </div>
        {outcome === 'sent' && (
          <span className="flex shrink-0 items-center gap-0.5 self-center text-xs font-semibold text-mint-ink">
            {draft('查看', 'View')}
            <ArrowUpRight className="size-3.5" />
          </span>
        )}
        {outcome === 'failed' && (
          <span className="flex shrink-0 items-center gap-1 self-center text-xs font-semibold text-mint-ink">
            <RotateCcw className="size-3.5" />
            {draft('重试', 'Retry')}
          </span>
        )}
      </div>
    </div>
  );
}

/** A one-time tip on the 工作台 tab after the change: the ＋ left the center. */
export function NewSessionTip() {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  const name = mainBot ? displayName(mainBot) : '';
  return (
    <div className="flex items-start gap-3 rounded-2xl border border-cyan/30 bg-cyan/5 px-3.5 py-3 text-xs leading-relaxed">
      <span className="font-semibold text-cyan-ink">{draft('新会话', 'New session')}</span>
      <span className="min-w-0 flex-1 text-foreground">
        {draft(
          `从项目开始：点项目右边的 ⋯，选「新建会话」。也可以直接跟${name}说要做什么，它会开会话去做。`,
          `Start one from a project: tap ⋯ on its row and choose New session. Or tell ${name} what to do and it opens one.`,
        )}
      </span>
      <X className="size-3.5 shrink-0 text-muted" />
    </div>
  );
}
