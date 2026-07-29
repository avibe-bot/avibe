import { apiFetch } from './apiFetch';

export type VoiceTelemetryOutcome =
  | 'success'
  | 'fallback'
  | 'cancelled'
  | 'empty'
  | 'failed'
  | 'timeout'
  | 'too_large'
  | 'unavailable';

export type VoiceTelemetryEvent = {
  event: 'segment_transcription' | 'dictation_finalized' | 'dictation_inserted';
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
