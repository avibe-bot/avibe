import {
  finalizeVoiceDictation,
  VOICE_SETTLE_RETRY_WINDOW_MS,
  VoiceTranscriptionError,
  type VoiceTranscriptionSegment,
} from './voiceTranscription';
import { claimVoiceTake, readVoiceTakeAudio, updateVoiceTake, type VoiceTake } from './voiceTakeStore';

// Transcribing a take kept on the device (lib/voiceTakeStore) that no surface holds in memory:
// after a reload, or after the surface it was spoken on went away. Its stored audio is sent
// again through whichever path works now (the cloud, then the local relay), under its own
// dictation id, and the text is kept with it until the reader inserts or sends it.

const running = new Map<string, Promise<string>>();
const listeners = new Set<() => void>();

const notify = () => {
  for (const listener of listeners) listener();
};

/** Whether a kept take is being transcribed in this document now. */
export const isRetryingVoiceTake = (id: string): boolean => running.has(id);

export function subscribeVoiceTakeRetries(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The segments a kept take can be finalized from: its stored audio, in order, then the last
 * segment or a finalize-only marker for a take the page lost while it recorded. Only a segment
 * recorded as empty may lack audio: one whose bytes never reached the device ends what can be
 * transcribed there (`truncated`), and is never taken for the end of the take.
 */
const keptVoiceSegments = (
  take: Pick<VoiceTake, 'segments'>,
  audio: Map<number, Blob | null>,
): { segments: VoiceTranscriptionSegment[]; truncated: boolean } => {
  const ordered = [...take.segments].sort((left, right) => left.sequence - right.sequence);
  const segments: VoiceTranscriptionSegment[] = [];
  let truncated = false;
  for (const segment of ordered) {
    const blob = audio.get(segment.sequence) ?? null;
    if (!blob && !segment.empty) {
      truncated = true;
      break;
    }
    segments.push({
      blob,
      sequence: segment.sequence,
      final: segment.final,
      durationMs: segment.durationMs,
      ...(segment.overlapMs ? { overlapMs: segment.overlapMs } : {}),
    });
    if (segment.final) break;
  }
  if (segments.length && !segments.at(-1)?.final) {
    segments.push({ blob: null, sequence: segments.length, final: true, durationMs: 0 });
  }
  return { segments, truncated };
};

/** Whether a kept take holds audio to transcribe at all. */
export const keptVoiceTakeHasAudio = (take: Pick<VoiceTake, 'segments'>): boolean => (
  take.segments.some((segment) => !segment.empty)
);

/**
 * Transcribes a kept take from its stored audio. Its text is kept with it (`transcribed`) for
 * the reader to insert or send; a failure leaves it `pending`. One run per take at a time, and
 * none while any surface (in this document or another) holds it: that surface lands its words,
 * and this resolves '' without a request. The result lands only on the take as it was read: if
 * it was written meanwhile, the newer record stands.
 */
export function retryKeptVoiceTake(take: VoiceTake): Promise<string> {
  const existing = running.get(take.id);
  if (existing) return existing;
  const read = take.revision ?? 0;
  const run = (async () => {
    const claimed = await claimVoiceTake(take.id, async () => {
      const { segments, truncated } = keptVoiceSegments(take, await readVoiceTakeAudio(take.id));
      if (!segments.some((segment) => segment.blob)) throw new VoiceTranscriptionError('failed');
      try {
        const result = await finalizeVoiceDictation(segments, { dictationId: take.id, before: '', after: '' }, {
          retryUntil: Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS,
        });
        await updateVoiceTake({ ...take, status: 'transcribed', text: result.text, truncated, updatedAt: Date.now() }, undefined, read);
        return result.text;
      } catch (error) {
        await updateVoiceTake({ ...take, status: 'pending', text: undefined, updatedAt: Date.now() }, undefined, read);
        throw error;
      }
    });
    return claimed ? claimed.value : '';
  })();
  running.set(take.id, run);
  notify();
  void run.then(() => undefined, () => undefined).finally(() => {
    running.delete(take.id);
    notify();
  });
  return run;
}
