import { useEffect, useReducer, useRef, useState } from 'react';
import { AudioLines, Loader2, MicOff, PenLine, RotateCcw, Send, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import {
  deleteVoiceTake,
  heldVoiceTakes,
  isVoiceTakeLive,
  readVoiceTakes,
  subscribeVoiceTakes,
  voiceTakeDurationMs,
  type VoiceTake,
} from '@/lib/voiceTakeStore';
import {
  isRetryingVoiceTake,
  keptVoiceTakeHasAudio,
  retryKeptVoiceTake,
  subscribeVoiceTakeRetries,
} from '@/lib/voiceTakeRetry';
import { Button } from '../ui/button';

// The takes kept on this device whose words have not landed yet (lib/voiceTakeStore), as small
// items above the composer of the conversation they were spoken into (ui/design:
// composer/pending-voice/*). One with no text yet retries on its own when the page comes back
// online or to the front, and on request; one transcribed waits to be put in the draft or sent.
// Each stays until the reader acts: nothing here discards a take by itself.

const clock = (ms: number) => {
  const seconds = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
};

/** One kept take as drawn: what it holds, and what can be done with it. */
export type PendingVoiceItemProps = {
  take: Pick<VoiceTake, 'status' | 'text' | 'label' | 'intent' | 'segments' | 'truncated'>;
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
 *  draft, send it, or discard). Props only, so a design frame draws it as shipped. */
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
        'flex w-full flex-col gap-1.5 rounded-2xl border bg-surface py-2 pr-2 pl-3.5 shadow-xl',
        ready ? 'border-mint/35' : 'border-gold/45',
      )}
    >
      <div className="flex min-w-0 items-center gap-2 text-xs">
        {ready
          ? <AudioLines className="size-4 shrink-0 text-mint-ink" />
          : <MicOff className="size-4 shrink-0 text-gold-ink" />}
        <span className="shrink-0 font-semibold text-foreground">
          {ready ? t('voice.pending.ready') : t('voice.pending.title')}
        </span>
        <span className="shrink-0 font-mono text-muted">{clock(voiceTakeDurationMs(take))}</span>
        {take.label && <span className="min-w-0 truncate text-muted before:mr-1 before:content-['·']">{take.label}</span>}
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

/** The kept takes for `target` that no live surface of this page holds, kept current. */
function useKeptVoiceTakes(target: string | null): VoiceTake[] {
  const [read, setRead] = useState<{ target: string; takes: VoiceTake[] } | null>(null);
  const [, refresh] = useReducer((count: number) => count + 1, 0);
  useEffect(() => {
    if (!target) return;
    let alive = true;
    const read = () => {
      // A take another document holds (still recording or finalizing it, or recovering it) is
      // that document's; one this document is recovering stays, with its progress.
      void Promise.all([readVoiceTakes(target), heldVoiceTakes()]).then(([found, held]) => {
        if (alive) setRead({ target, takes: found.filter((take) => !held.has(take.id) || isRetryingVoiceTake(take.id)) });
      });
    };
    read();
    const offTakes = subscribeVoiceTakes(read);
    const offRetries = subscribeVoiceTakeRetries(refresh);
    // Back to the page: another document (the pet, another tab) may have kept a take meanwhile.
    const onVisible = () => {
      if (document.visibilityState === 'visible') read();
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('online', read);
    return () => {
      alive = false;
      offTakes();
      offRetries();
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('online', read);
    };
  }, [target]);
  const takes = read && read.target === target ? read.takes : [];
  return takes.filter((take) => !isVoiceTakeLive(take.id));
}

// Takes this document has already tried on its own once since it loaded.
const triedOnLoad = new Set<string>();

/** Transcribes the kept takes that have no text yet: each once when this page first shows it
 *  (a reload is a return), and all of them again whenever the page comes back online or to
 *  the front. Never while the browser reports no network. */
function useAutoRetry(takes: VoiceTake[], onFailed: (id: string) => void) {
  const retryAll = useRef<(only?: (take: VoiceTake) => boolean) => void>(() => undefined);
  useEffect(() => {
    retryAll.current = (only) => {
      if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
      for (const take of takes) {
        if (take.status === 'transcribed' || isRetryingVoiceTake(take.id) || !keptVoiceTakeHasAudio(take)) continue;
        if (only && !only(take)) continue;
        void retryKeptVoiceTake(take).catch(() => onFailed(take.id));
      }
    };
    retryAll.current((take) => {
      if (triedOnLoad.has(take.id)) return false;
      triedOnLoad.add(take.id);
      return true;
    });
  });
  useEffect(() => {
    const onOnline = () => retryAll.current();
    const onVisible = () => {
      if (document.visibilityState === 'visible') retryAll.current();
    };
    window.addEventListener('online', onOnline);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('online', onOnline);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);
}

/**
 * The pending voice items for one conversation's composer. `onInsert` puts words into its draft
 * (false when the draft cannot take them now); `onSend` sends them as a spoken message, offered
 * for takes that were meant to be sent.
 */
export function PendingVoiceTakes({
  target,
  onInsert,
  onSend,
}: {
  target: string | null;
  onInsert: (text: string) => boolean;
  onSend?: (text: string) => Promise<boolean>;
}) {
  const takes = useKeptVoiceTakes(target);
  const [failed, setFailed] = useState<ReadonlySet<string>>(() => new Set());
  const markFailed = (id: string) => setFailed((current) => new Set(current).add(id));
  useAutoRetry(takes, markFailed);
  if (!takes.length) return null;
  return (
    <div className="flex w-full max-w-xl flex-col gap-2">
      {takes.map((take) => {
        const text = take.text?.trim() ?? '';
        return (
          <PendingVoiceItem
            key={take.id}
            take={take}
            retrying={isRetryingVoiceTake(take.id)}
            failed={failed.has(take.id)}
            onRetry={() => {
              void retryKeptVoiceTake(take).then(
                () => setFailed((current) => {
                  const next = new Set(current);
                  next.delete(take.id);
                  return next;
                }),
                () => markFailed(take.id),
              );
            }}
            onDiscard={() => void deleteVoiceTake(take.id)}
            onInsert={() => {
              if (text && onInsert(text)) void deleteVoiceTake(take.id);
            }}
            onSend={onSend && take.intent === 'send'
              ? () => {
                  if (!text) return;
                  void onSend(text).then((sent) => {
                    if (sent) void deleteVoiceTake(take.id);
                  });
                }
              : undefined}
          />
        );
      })}
    </div>
  );
}
