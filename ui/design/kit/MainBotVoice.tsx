// Press and hold the main Bot to speak to it (owner decisions, 2026-11-02). While
// held, the shipped dictation pipeline (VoiceRealtimeSession) streams a live
// transcript into the card; sliding up into the cancel target arms cancel;
// releasing sends the final text straight to the main Bot's conversation, or
// cancels. There is no time limit: a long transcript scrolls inside the card, and
// a take cut off by the network or the speech service keeps what was heard, to
// send or retry. Afterwards one toast above the bar says what happened.
//
// `VoiceCard` and `VoiceCancelTarget` are the parts every hold-to-talk shares (the
// composer's hold-to-talk anchors them above its composer); `VoiceHoldOverlay`
// anchors them above the tab bar for the main Bot.
import type { ReactNode } from 'react';
import { AlertTriangle, ArrowUpRight, ChevronUp, CloudOff, Loader2, MessagesSquare, Mic, MicOff, RotateCcw, Send, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { BotAvatar } from '@/components/ui/bot-avatar';
import { Button } from '@/components/ui/button';
import { useBots } from '@/context/BotsContext';
import type { BotAvatar as BotAvatarSpec } from '@/lib/bots';

import { useDraft } from './draft';

export type VoicePhase = 'listening' | 'recording' | 'armed' | 'settling' | 'interrupted';

/** Who the take goes to. `title` replaces the default "说给{name}" (a Session: 发到「…」); with no
 *  avatar the card shows a neutral conversation icon. */
export type VoiceTarget = { name: string; avatar?: BotAvatarSpec | null; title?: string };

// A level meter's resting shape; the build drives it from the microphone level.
const LEVELS = ['h-1', 'h-1.5', 'h-2.5', 'h-2', 'h-3', 'h-2', 'h-3.5', 'h-3', 'h-1.5', 'h-2.5', 'h-3.5', 'h-2', 'h-1.5', 'h-2', 'h-3', 'h-1.5', 'h-1'];

const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

/** The take as it is heard: who it goes to, the clock, the live transcript (the stable part dark,
 *  the tail the recognizer may still revise light), and the level meter, "正在整理…", or — when the
 *  take was cut off — what happened and what can be done (`actions`, default 重试 / 发送). A long
 *  transcript scrolls inside the card and stays on its latest line; `long` fades the top edge to
 *  say there is more above. */
export function VoiceCard({
  to,
  phase,
  text = '',
  stash = '',
  seconds,
  long = false,
  actions,
}: {
  to: VoiceTarget;
  phase: VoicePhase;
  text?: string;
  stash?: string;
  seconds: number;
  long?: boolean;
  actions?: ReactNode;
}) {
  const { t } = useTranslation();
  const draft = useDraft();
  const armed = phase === 'armed';
  const interrupted = phase === 'interrupted';
  return (
    <div
      className={clsx(
        'flex w-full flex-col gap-3 rounded-3xl border bg-surface p-4 shadow-xl transition',
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
        <span className="min-w-0 truncate font-semibold text-foreground">{to.title ?? draft(`说给${to.name}`, `To ${to.name}`)}</span>
        <span className="flex-1" />
        {interrupted ? (
          <span className="rounded-full bg-gold-soft px-2 py-0.5 font-semibold text-gold-ink">{draft('已中断', 'Cut off')}</span>
        ) : (
          <span className={clsx('font-mono', armed ? 'text-destructive-ink' : 'text-muted')}>{clock(seconds)}</span>
        )}
      </div>
      <div className="relative">
        {/* Bottom-aligned, so an overflowing transcript rests on its newest line; the build keeps it
            scrolled there while the take runs and lets it scroll once the finger is off. */}
        <div className="flex max-h-40 flex-col justify-end overflow-hidden">
          <p className={clsx('min-h-14 shrink-0 text-lg leading-relaxed', armed && 'line-through decoration-destructive/60')}>
            {text || stash ? (
              <>
                <span className="text-foreground">{text}</span>
                <span className="text-muted">{stash}</span>
              </>
            ) : (
              <span className="text-muted">{t('pet.voice.listening')}</span>
            )}
          </p>
        </div>
        {long && <div className="pointer-events-none absolute inset-x-0 top-0 h-10 bg-linear-to-b from-surface to-transparent" aria-hidden="true" />}
      </div>
      {phase === 'settling' && (
        <div className="flex items-center gap-2 text-xs text-mint-ink">
          <Loader2 className="size-3.5 animate-spin" />
          {t('pet.voice.finishing')}
        </div>
      )}
      {interrupted && (
        <div className="flex flex-col gap-3">
          <div className="flex items-start gap-2 text-xs leading-relaxed text-foreground">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-gold-ink" />
            {draft('连接断了，已经听到的都在上面。可以直接发送，或者重试转写这段录音。', 'The connection dropped. Everything heard so far is above: send it, or retry transcribing the recording.')}
          </div>
          <div className="flex justify-end gap-2">
            {actions ?? (
              <>
                <Button type="button" variant="outline" size="sm" className="gap-1.5">
                  <RotateCcw className="size-3.5" />
                  {draft('重试', 'Retry')}
                </Button>
                <Button type="button" variant="brand" size="sm" className="gap-1.5">
                  <Send className="size-3.5" />
                  {draft('发送', 'Send')}
                </Button>
              </>
            )}
          </div>
        </div>
      )}
      {(phase === 'listening' || phase === 'recording' || armed) && (
        <div className="flex h-4 items-center gap-1" aria-hidden="true">
          {LEVELS.map((level, index) => (
            <span key={index} className={clsx('w-1 rounded-full', level, armed ? 'bg-muted' : 'bg-mint')} />
          ))}
        </div>
      )}
    </div>
  );
}

/** Where the finger goes to cancel, and the hint. Armed, it fills red and says 松开取消. */
export function VoiceCancelTarget({ armed, hint }: { armed: boolean; hint?: ReactNode }) {
  const draft = useDraft();
  return (
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
        {armed ? draft('松开取消', 'Release to cancel') : hint ?? (
          <>
            <ChevronUp className="size-3.5" />
            {draft('上滑取消 · 松开发送', 'Slide up to cancel · release to send')}
          </>
        )}
      </span>
    </div>
  );
}

/** The main Bot's hold-to-talk, anchored above the tab bar. */
export function VoiceHoldOverlay({
  phase,
  text,
  stash,
  seconds,
  long,
}: {
  phase: VoicePhase;
  text?: string;
  stash?: string;
  seconds: number;
  long?: boolean;
}) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  if (!mainBot) return null;
  const name = displayName(mainBot);
  const holding = phase === 'listening' || phase === 'recording' || phase === 'armed';
  return (
    <div className="md:hidden" role="dialog" aria-modal="true" aria-label={draft(`说给${name}`, `Speak to ${name}`)}>
      <div className="fixed inset-x-0 top-0 bottom-(--mobile-nav-height) z-40 bg-background/80 backdrop-blur-sm" />
      <div className="fixed inset-x-0 bottom-(--mobile-nav-height) z-50 flex flex-col items-center gap-5 px-5 pb-10">
        <VoiceCard to={{ name, avatar: mainBot.avatar }} phase={phase} text={text} stash={stash} seconds={seconds} long={long} />
        {holding && <VoiceCancelTarget armed={phase === 'armed'} />}
      </div>
    </div>
  );
}

/** The small mark a dictated message carries, in the conversation and in the sent toast. The build
 *  shows it on a user message whose metadata says it was dictated. */
export function VoiceMark() {
  const draft = useDraft();
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-mint-soft px-1.5 py-0.5 text-2xs font-semibold text-mint-ink">
      <Mic className="size-3" />
      {draft('语音', 'Voice')}
    </span>
  );
}

/** Where a frame puts the mark on a shipped message bubble it cannot reach into: an absolute
 *  layer at the bubble's corner. */
export function VoiceMarkLayer({ className }: { className: string }) {
  return (
    <div className={clsx('pointer-events-none fixed z-50', className)}>
      <VoiceMark />
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
              <span className="flex items-center gap-1.5 font-semibold text-foreground">
                {draft(`已发给${name}`, `Sent to ${name}`)}
                <VoiceMark />
              </span>
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
              <span className="text-muted">{draft('一个字都没识别出来，录音还在，可以重试转写。', 'Nothing was recognized; the recording is kept, so you can retry.')}</span>
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
