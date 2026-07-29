import { apiFetch } from './apiFetch';

export type VoiceTelemetryOutcome =
  | 'success'
  | 'fallback'
  | 'empty'
  | 'failed'
  | 'timeout'
  | 'too_large'
  | 'unavailable';

export type VoiceTelemetryEvent = {
  event: 'segment_transcription' | 'dictation_finalized';
  outcome: VoiceTelemetryOutcome;
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
  retry?: boolean;
};

type TelemetryFetch = (path: string, init?: RequestInit) => Promise<Response>;

const browserFamily = (): string => {
  if (typeof navigator === 'undefined') return 'unknown';
  const userAgent = navigator.userAgent;
  if (/Edg\//.test(userAgent)) return 'edge';
  if (/Firefox\//.test(userAgent)) return 'firefox';
  if (/Chrome\//.test(userAgent)) return 'chrome';
  if (/Safari\//.test(userAgent)) return 'safari';
  return 'other';
};

export const emitVoiceTelemetry = (
  event: VoiceTelemetryEvent,
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
