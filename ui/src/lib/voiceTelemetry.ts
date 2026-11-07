import { apiFetch } from './apiFetch';
import type { VoiceCaptureClass, VoiceCaptureEntry, VoiceCaptureTraceEvent } from './voiceCaptureTrace';

export type VoiceTelemetryOutcome =
  | 'success'
  | 'fallback'
  | 'cancelled'
  | 'empty'
  | 'failed'
  | 'timeout'
  | 'too_large'
  | 'unavailable'
  // How a press-and-hold ended (`dictation_finalized`, entry `hold`): sent; nothing to send because
  // the take was silent (`too_short`), its graph rendered no frames (`no_frames`), or the system
  // held the microphone (`muted`); the microphone refused (`denied`) or its permission asked
  // (`asking`); cancelled, kept for later (`kept`), or interrupted with its words kept.
  | 'sent'
  | 'too_short'
  | 'no_frames'
  | 'muted'
  | 'denied'
  | 'asking'
  | 'kept'
  | 'interrupted';

export type VoiceTelemetryEvent = {
  event: 'segment_transcription' | 'dictation_finalized' | 'dictation_inserted' | 'dictation_gated';
  outcome: VoiceTelemetryOutcome;
  dictationId?: string;
  path?: 'cloud' | 'local';
  providerStage?: 'token' | 'upload' | 'refresh' | 'response' | 'finalization';
  sizeBytes?: number;
  mimeType?: string;
  durationMs?: number;
  elapsedMs?: number;
  httpStatus?: number;
  attemptCount?: number;
  segmentCount?: number;
  failedSegmentCount?: number;
  backlogAtStop?: number;
  totalDurationMs?: number;
  stopToInsertionMs?: number;
  realtime?: boolean;
  firstPreviewMs?: number;
  stopToFinalMs?: number;
  retry?: boolean;
  /** The silence gate's evidence (`dictation_gated`): readings that reached it, and the loudest. */
  voicedReadings?: number;
  peakDbfs?: number;
  /** Where the take started, and what its microphone delivered (lib/voiceCaptureTrace). */
  entry?: VoiceCaptureEntry;
  captureClass?: VoiceCaptureClass;
  /** Why an interrupted hold stopped. */
  interruptReason?: 'dropped' | 'failed' | 'system' | 'send-failed';
};

type TelemetryFetch = (path: string, init?: RequestInit) => Promise<Response>;

export const browserFamilyFromUserAgent = (userAgent: string): string => {
  if (/Edg(?:iOS|A)?\//.test(userAgent)) return 'edge';
  if (/Firefox\/|FxiOS\//.test(userAgent)) return 'firefox';
  if (/Chrome\/|CriOS\//.test(userAgent)) return 'chrome';
  if (/Safari\//.test(userAgent)) return 'safari';
  return 'other';
};

const browserFamily = (): string => (
  typeof navigator === 'undefined'
    ? 'unknown'
    : browserFamilyFromUserAgent(navigator.userAgent)
);

export const emitVoiceTelemetry = (
  event: VoiceTelemetryEvent | VoiceCaptureTraceEvent,
  telemetryFetch: TelemetryFetch = apiFetch,
): void => {
  if (typeof window === 'undefined') return;
  void telemetryFetch('/api/asr/telemetry', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...event, browserFamily: browserFamily() }),
    keepalive: true,
  }).catch(() => {
    // Metrics must never delay or fail voice input.
  });
};
