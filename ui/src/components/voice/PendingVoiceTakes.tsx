import { desktopVoiceLock } from '@/lib/desktopVoiceLock';
import { Suspense, useEffect, useReducer, useRef, useState } from 'react';
import { lazyPage } from '@/lib/lazyPage';

import {
  deleteVoiceTake,
  heldVoiceTakes,
  isVoiceTakeLive,
  landVoiceTake,
  readVoiceTakes,
  subscribeVoiceTakes,
  voiceTakeFreed,
  type VoiceTake,
} from '@/lib/voiceTakeStore';
import {
  isRetryingVoiceTake,
  keptVoiceTakeHasAudio,
  retryKeptVoiceTake,
  subscribeVoiceTakeRetries,
} from '@/lib/voiceTakeRetry';
import { askVoiceConsent, knownVoiceConsent, useVoiceConsent } from '@/lib/voiceConsent';

// The takes kept on this device whose words have not landed yet (lib/voiceTakeStore), as small
// items above the composer of the conversation they were spoken into (ui/design:
// composer/pending-voice/*). One with no text yet retries on its own when the page comes back
// online or to the front, and on request; one transcribed waits to be put in the draft or sent.
// Each stays until the reader acts: nothing here discards a take by itself.

// Only the cards are deferred: storage, retry admission and lock guards stay eager.
const PendingVoiceItem = lazyPage(() => import('./PendingVoiceItem').then((module) => module.PendingVoiceItem));

/** The kept takes for `target` that no live surface of this page holds, kept current. */
function useKeptVoiceTakes(target: string | null): VoiceTake[] {
  const [read, setRead] = useState<{ target: string; takes: VoiceTake[] } | null>(null);
  const [, refresh] = useReducer((count: number) => count + 1, 0);
  useEffect(() => {
    if (!target) return;
    let alive = true;
    const stop = new AbortController();
    const waiting = new Set<string>();
    const read = () => {
      // A take another document holds (still recording or finalizing it, or recovering it) is
      // that document's; one this document is recovering stays, with its progress.
      void Promise.all([readVoiceTakes(target), heldVoiceTakes()]).then(([found, held]) => {
        if (!alive) return;
        setRead({ target, takes: found.filter((take) => !held.has(take.id) || isRetryingVoiceTake(take.id)) });
        // It comes back here once that document lets go of it, or goes away without a word
        // (closed, crashed): wait on its lock rather than on a message it may never send.
        for (const take of found) {
          if (!held.has(take.id) || waiting.has(take.id) || isVoiceTakeLive(take.id) || isRetryingVoiceTake(take.id)) continue;
          waiting.add(take.id);
          void voiceTakeFreed(take.id, stop.signal).then(() => {
            waiting.delete(take.id);
            if (alive) read();
          });
        }
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
      stop.abort();
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
 *  (a reload is a return; not one the reader set aside, whose words they stopped waiting for),
 *  and all of them again whenever the page comes back online or to the front. Never while the
 *  browser reports no network. */
function useAutoRetry(takes: VoiceTake[], onFailed: (id: string) => void) {
  const retryAll = useRef<(only?: (take: VoiceTake) => boolean) => void>(() => undefined);
  // Re-rendered when this account's voice answer is known or changes: a kept take is sent again
  // only after it said yes (ui/design: composer/voice-consent/*).
  useVoiceConsent();
  useEffect(() => {
    retryAll.current = (only) => {
      if (!desktopVoiceLock.allowed()) return;
      if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
      if (knownVoiceConsent() !== 'granted') return;
      for (const take of takes) {
        if (take.status === 'transcribed' || isRetryingVoiceTake(take.id) || !keptVoiceTakeHasAudio(take)) continue;
        if (only && !only(take)) continue;
        // An online/visible attempt also satisfies the initial attempt. Otherwise
        // a render with the old pending row can start it again as that run ends.
        triedOnLoad.add(take.id);
        void retryKeptVoiceTake(take).catch(() => onFailed(take.id));
      }
    };
    retryAll.current((take) => {
      if (take.setAside || triedOnLoad.has(take.id)) return false;
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
    <Suspense fallback={null}>
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
                if (!desktopVoiceLock.allowed()) return;
                if (knownVoiceConsent() !== 'granted') {
                  askVoiceConsent();
                  return;
                }
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
              // Once, across documents: another tab showing the same take cannot land it again.
              onInsert={() => {
                if (desktopVoiceLock.allowed() && text) void landVoiceTake(take.id, () => desktopVoiceLock.allowed() && onInsert(text));
              }}
              onSend={onSend && take.intent === 'send'
                ? () => {
                    if (desktopVoiceLock.allowed() && text) void landVoiceTake(take.id, () => desktopVoiceLock.allowed() ? onSend(text) : Promise.resolve(false));
                  }
                : undefined}
            />
          );
        })}
      </div>
    </Suspense>
  );
}
