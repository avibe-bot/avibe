import { AudioLines, FileHeadphone, FileX, Loader2, PenLine, RotateCcw, Send, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { voiceTakeDurationMs, type VoiceTake } from '@/lib/voiceTakeStore';
import { keptVoiceTakeHasAudio } from '@/lib/voiceTakeRetry';
import { Button } from '../ui/button';

const clock = (ms: number) => {
  const seconds = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
};

/** One kept take as drawn: what it holds, and what can be done with it. */
export type PendingVoiceItemProps = {
  take: Pick<VoiceTake, 'status' | 'text' | 'intent' | 'segments' | 'truncated'>;
  /** Being transcribed now. */
  retrying?: boolean;
  /** The last attempt failed (as opposed to never tried in this page). */
  failed?: boolean;
  onRetry?: () => void;
  onDiscard?: () => void;
  onInsert?: () => void;
  /** Absent where the words can only go into a draft. */
  onSend?: () => void;
};

/** A kept take: no text yet (retry, or discard), or text that has not landed (put it in the
 *  draft, send it, or discard). Props only, so a design frame draws it as shipped. It does not
 *  say who the take was spoken to: it sits above that conversation's own composer. Its icon says
 *  what is on the device: the words (mint), saved audio waiting for them (amber), or nothing
 *  that could be saved (amber). */
export function PendingVoiceItem({ take, retrying = false, failed = false, onRetry, onDiscard, onInsert, onSend }: PendingVoiceItemProps) {
  const { t } = useTranslation();
  const ready = take.status === 'transcribed' && Boolean(take.text?.trim());
  const hasAudio = keptVoiceTakeHasAudio(take);
  const note = retrying
    ? t('voice.pending.retrying')
    : !hasAudio
      ? t('voice.pending.lost')
      : failed
        ? t('voice.pending.failed')
        : t('voice.pending.waiting');
  const discard = (
    <Button type="button" variant="ghost" size="icon" className="size-8 shrink-0 text-muted" aria-label={t('voice.discard')} onClick={onDiscard}>
      <Trash2 className="size-4" />
    </Button>
  );
  return (
    <div
      role="status"
      className={clsx(
        'flex w-full flex-col gap-1.5 rounded-2xl border bg-surface py-2 pr-2 pl-3.5 shadow-elevation-2',
        ready ? 'border-mint/35' : 'border-gold/45',
      )}
    >
      <div className="flex min-w-0 items-center gap-2 text-xs">
        {ready
          ? <AudioLines className="size-4 shrink-0 text-mint-ink" />
          : hasAudio
            ? <FileHeadphone className="size-4 shrink-0 text-gold-ink" />
            : <FileX className="size-4 shrink-0 text-gold-ink" />}
        <span className="shrink-0 font-semibold text-foreground">
          {ready ? t('voice.pending.ready') : t('voice.pending.title')}
        </span>
        <span className="shrink-0 font-mono text-muted">{clock(voiceTakeDurationMs(take))}</span>
        <span className="flex-1" />
        {!ready && discard}
        {!ready && hasAudio && (
          <Button type="button" variant="outline" size="sm" className="shrink-0 gap-1.5" disabled={retrying} onClick={onRetry}>
            {retrying ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />}
            {t('voice.retry')}
          </Button>
        )}
      </div>
      {ready ? (
        <>
          <p className="line-clamp-2 pr-1.5 text-sm leading-relaxed text-foreground">{take.text}</p>
          {take.truncated && <p className="pr-1.5 text-xs leading-relaxed text-gold-ink">{t('voice.pending.truncated')}</p>}
          <div className="flex items-center justify-end gap-2">
            <span className="mr-auto -ml-2">{discard}</span>
            <Button type="button" variant={onSend ? 'ghost' : 'brand'} size="sm" className="gap-1.5" onClick={onInsert}>
              <PenLine className="size-3.5" />
              {t('voice.pending.insert')}
            </Button>
            {onSend && (
              <Button type="button" variant="brand" size="sm" className="gap-1.5" onClick={onSend}>
                <Send className="size-3.5" />
                {t('voice.send')}
              </Button>
            )}
          </div>
        </>
      ) : (
        <p className="pr-1.5 pb-0.5 text-xs leading-relaxed text-muted">{note}</p>
      )}
    </div>
  );
}

