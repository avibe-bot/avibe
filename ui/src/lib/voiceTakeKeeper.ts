import type { VoiceTranscriptionSegment } from './voiceTranscription';
import {
  deleteVoiceTake,
  saveVoiceTake,
  saveVoiceTakeAudio,
  setVoiceTakeLive,
  type VoiceTake,
  type VoiceTakeIntent,
  type VoiceTakeSegmentInfo,
} from './voiceTakeStore';

/** Where a kept take's words go, and how (lib/voiceTakeStore). */
export type VoiceTakeKeep = {
  target: string;
  intent: VoiceTakeIntent;
  /** Who it was spoken to, as its card said it. */
  label?: string | null;
};

/**
 * Keeps one take on the device while a surface records and transcribes it: its details and
 * audio as they are produced, its text once it is transcribed, until its words land or the
 * reader discards it. Writes go out in order; nothing written after the take ended can bring
 * it back. Every write is best effort (lib/voiceTakeStore).
 */
export class VoiceTakeKeeper {
  private readonly take: VoiceTake;
  private writes: Promise<unknown> = Promise.resolve();
  private ended = false;

  constructor(id: string, keep: VoiceTakeKeep) {
    const now = Date.now();
    this.take = {
      id,
      target: keep.target,
      intent: keep.intent,
      label: keep.label ?? null,
      createdAt: now,
      updatedAt: now,
      status: 'recording',
      segments: [],
    };
  }

  get id(): string {
    return this.take.id;
  }

  /** The take started recording: it is stored, and held by its surface. */
  begin(): void {
    setVoiceTakeLive(this.take.id, true);
    this.save();
  }

  /** One recorded segment (null audio: the finalize-only marker of a take that ended on a
   *  segment boundary). */
  segment(segment: Pick<VoiceTranscriptionSegment, 'sequence' | 'final' | 'durationMs' | 'overlapMs'>, audio: Blob | null): void {
    if (this.ended) return;
    const info: VoiceTakeSegmentInfo = {
      sequence: segment.sequence,
      final: segment.final,
      durationMs: segment.durationMs ?? 0,
      ...(segment.overlapMs ? { overlapMs: segment.overlapMs } : {}),
      ...(audio ? {} : { empty: true }),
    };
    this.take.segments = [...this.take.segments.filter((kept) => kept.sequence !== info.sequence), info];
    this.save();
    this.queue(() => saveVoiceTakeAudio(this.take.id, info.sequence, audio));
  }

  /** Recording ended without text yet. */
  pending(): void {
    if (this.ended) return;
    this.take.status = 'pending';
    delete this.take.text;
    this.save();
  }

  /** Transcribed: the text is kept until it lands. */
  transcribed(text: string): void {
    if (this.ended) return;
    this.take.status = 'transcribed';
    this.take.text = text;
    this.save();
  }

  /** Its words were inserted or sent, or the reader discarded it: deleted, after any write
   *  still queued. */
  forget(): void {
    if (this.ended) return;
    this.ended = true;
    // Held until it is gone: the pending voice items never see it in between.
    this.queue(async () => {
      await deleteVoiceTake(this.take.id);
      setVoiceTakeLive(this.take.id, false);
    });
  }

  /** Its surface is gone: the take stays on the device, for the pending voice items, once
   *  every write about it has landed. */
  release(): void {
    this.queue(async () => setVoiceTakeLive(this.take.id, false));
  }

  private save(): void {
    if (this.ended) return;
    this.take.updatedAt = Date.now();
    const snapshot: VoiceTake = { ...this.take, segments: [...this.take.segments] };
    this.queue(() => saveVoiceTake(snapshot));
  }

  private queue(write: () => Promise<unknown>): void {
    this.writes = this.writes.then(write, write).catch(() => undefined);
  }
}
