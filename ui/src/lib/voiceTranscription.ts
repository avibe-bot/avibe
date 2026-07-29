import { apiFetch } from './apiFetch';
import { avibeFetch, CloudUnavailableError } from './avibeFetch';
import {
  emitVoiceTelemetry,
  type VoiceTelemetryEvent,
  type VoiceTelemetryOutcome,
} from './voiceTelemetry';

export const VOICE_SEGMENT_MS = 60_000;
export const VOICE_AUDIO_BITS_PER_SECOND = 32_000;

const TRANSCRIPTION_TIMEOUT_MS = 130_000;

const EXTENSION_BY_MIME: Record<string, string> = {
  'audio/aac': 'aac',
  'audio/mp4': 'mp4',
  'audio/mpeg': 'mp3',
  'audio/ogg': 'ogg',
  'audio/opus': 'opus',
  'audio/wav': 'wav',
  'audio/webm': 'webm',
  'audio/x-m4a': 'm4a',
};

const RECORDER_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/ogg;codecs=opus',
  'audio/webm',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
];

export type VoiceTranscriptionErrorCode =
  | 'empty'
  | 'failed'
  | 'timeout'
  | 'too_large'
  | 'unavailable';

export class VoiceTranscriptionError extends Error {
  readonly code: VoiceTranscriptionErrorCode;
  readonly status?: number;

  constructor(code: VoiceTranscriptionErrorCode, options: { cause?: unknown; status?: number } = {}) {
    super(code, { cause: options.cause });
    this.name = 'VoiceTranscriptionError';
    this.code = code;
    this.status = options.status;
  }
}

type VoiceFetch = (path: string, init?: RequestInit) => Promise<Response>;

export type VoiceTranscriptionDependencies = {
  cloudFetch?: VoiceFetch;
  localFetch?: VoiceFetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  durationMs?: number;
  attemptCount?: number;
  telemetry?: (event: VoiceTelemetryEvent) => void;
};

export type VoiceTranscriptionSegment = {
  blob: Blob;
  durationMs?: number;
  attemptCount?: number;
  text?: string;
  error?: unknown;
};

const normalizedMimeType = (blob: Blob): string =>
  blob.type.split(';', 1)[0]?.trim().toLowerCase() || 'audio/webm';

export const voiceRecordingFileName = (blob: Blob): string =>
  `voice.${EXTENSION_BY_MIME[normalizedMimeType(blob)] ?? 'webm'}`;

export const preferredRecorderMimeType = (): string | undefined => {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') {
    return undefined;
  }
  return RECORDER_MIME_TYPES.find((mimeType) => MediaRecorder.isTypeSupported(mimeType));
};

const requestTimeout = (durationMs: number, externalSignal?: AbortSignal) => {
  const controller = new AbortController();
  const abortFromExternal = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) {
    abortFromExternal();
  } else {
    externalSignal?.addEventListener('abort', abortFromExternal, { once: true });
  }
  const timer = globalThis.setTimeout(
    () => controller.abort(new DOMException('transcription timed out', 'TimeoutError')),
    durationMs,
  );
  return {
    signal: controller.signal,
    cancel: () => {
      globalThis.clearTimeout(timer);
      externalSignal?.removeEventListener('abort', abortFromExternal);
    },
  };
};

const isTimeoutError = (error: unknown): boolean =>
  error instanceof DOMException && (error.name === 'AbortError' || error.name === 'TimeoutError');

const normalizeTranscriptionError = (
  error: unknown,
  signal: AbortSignal,
): VoiceTranscriptionError => {
  if (error instanceof VoiceTranscriptionError) return error;
  if (isTimeoutError(error) || signal.aborted) {
    return new VoiceTranscriptionError('timeout', { cause: error });
  }
  return new VoiceTranscriptionError('failed', { cause: error });
};

const telemetryOutcome = (error: VoiceTranscriptionError): VoiceTelemetryOutcome => error.code;

const responseError = async (response: Response): Promise<VoiceTranscriptionError> => {
  const payload = (await response.json().catch(() => null)) as { error?: unknown } | null;
  const upstreamCode = typeof payload?.error === 'string' ? payload.error : '';
  if (response.status === 413 || upstreamCode === 'file_too_large') {
    return new VoiceTranscriptionError('too_large', { status: response.status });
  }
  if (response.status === 504 || upstreamCode === 'transcription_timeout') {
    return new VoiceTranscriptionError('timeout', { status: response.status });
  }
  if (
    response.status === 503
    || upstreamCode === 'asr_not_configured'
    || upstreamCode === 'asr_unavailable'
  ) {
    return new VoiceTranscriptionError('unavailable', { status: response.status });
  }
  return new VoiceTranscriptionError('failed', { status: response.status });
};

const responseText = async (response: Response): Promise<string> => {
  if (!response.ok) throw await responseError(response);
  const payload = (await response.json().catch(() => null)) as { text?: unknown } | null;
  const text = typeof payload?.text === 'string' ? payload.text.trim() : '';
  if (!text) throw new VoiceTranscriptionError('empty', { status: response.status });
  return text;
};

const readBlobAsBase64 = async (blob: Blob): Promise<string> => {
  if (typeof FileReader === 'undefined') {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const dataUrl = String(reader.result || '');
      const comma = dataUrl.indexOf(',');
      resolve(comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl);
    };
    reader.readAsDataURL(blob);
  });
};

const transcribeLocally = async (
  blob: Blob,
  localFetch: VoiceFetch,
  signal: AbortSignal,
): Promise<string> => {
  try {
    const data = await readBlobAsBase64(blob);
    const response = await localFetch('/api/asr/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: voiceRecordingFileName(blob),
        mime: normalizedMimeType(blob),
        data,
      }),
      signal,
    });
    return await responseText(response);
  } catch (error) {
    throw normalizeTranscriptionError(error, signal);
  }
};

export const transcribeVoiceBlob = async (
  blob: Blob,
  dependencies: VoiceTranscriptionDependencies = {},
): Promise<string> => {
  const cloudFetch = dependencies.cloudFetch ?? avibeFetch;
  const localFetch = dependencies.localFetch ?? apiFetch;
  const timeoutMs = dependencies.timeoutMs ?? TRANSCRIPTION_TIMEOUT_MS;
  const telemetry = dependencies.telemetry ?? emitVoiceTelemetry;
  const attemptCount = dependencies.attemptCount ?? 1;
  const timeout = requestTimeout(timeoutMs, dependencies.signal);
  const report = (
    path: 'cloud' | 'local',
    providerStage: NonNullable<VoiceTelemetryEvent['providerStage']>,
    outcome: VoiceTelemetryOutcome,
    startedAt: number,
    error?: VoiceTranscriptionError,
  ) => {
    try {
      telemetry({
        event: 'segment_transcription',
        path,
        providerStage,
        outcome,
        sizeBytes: blob.size,
        mimeType: normalizedMimeType(blob),
        durationMs: dependencies.durationMs,
        elapsedMs: Date.now() - startedAt,
        httpStatus: error?.status,
        attemptCount,
      });
    } catch {
      // Instrumentation cannot change transcription behavior.
    }
  };
  const cloudStartedAt = Date.now();
  try {
    const form = new FormData();
    form.set('file', blob, voiceRecordingFileName(blob));
    const response = await cloudFetch('/api/cloud/audio/transcriptions', {
      method: 'POST',
      body: form,
      signal: timeout.signal,
    });
    const text = await responseText(response);
    report('cloud', 'response', 'success', cloudStartedAt);
    return text;
  } catch (error) {
    if (error instanceof CloudUnavailableError && !error.uploadStarted) {
      report('cloud', 'token', 'fallback', cloudStartedAt);
      const localStartedAt = Date.now();
      try {
        const text = await transcribeLocally(blob, localFetch, timeout.signal);
        report('local', 'response', 'success', localStartedAt);
        return text;
      } catch (localError) {
        const normalized = normalizeTranscriptionError(localError, timeout.signal);
        report(
          'local',
          normalized.status == null ? 'upload' : 'response',
          telemetryOutcome(normalized),
          localStartedAt,
          normalized,
        );
        throw normalized;
      }
    }
    const normalized = normalizeTranscriptionError(error, timeout.signal);
    const providerStage = error instanceof CloudUnavailableError
      ? 'refresh'
      : normalized.status == null
        ? 'upload'
        : 'response';
    report('cloud', providerStage, telemetryOutcome(normalized), cloudStartedAt, normalized);
    throw normalized;
  } finally {
    timeout.cancel();
  }
};

export const transcribeVoiceSegments = async (
  segments: VoiceTranscriptionSegment[],
  dependencies: VoiceTranscriptionDependencies & {
    concurrency?: number;
    transcribe?: (blob: Blob) => Promise<string>;
  } = {},
): Promise<void> => {
  const {
    concurrency: requestedConcurrency = 2,
    transcribe: customTranscribe,
    ...transcriptionDependencies
  } = dependencies;
  const queue = segments.filter((segment) => !segment.text);
  const concurrency = Math.max(1, Math.floor(requestedConcurrency));
  const worker = async () => {
    let segment = queue.shift();
    while (segment) {
      segment.error = undefined;
      segment.attemptCount = (segment.attemptCount ?? 0) + 1;
      try {
        segment.text = customTranscribe
          ? await customTranscribe(segment.blob)
          : await transcribeVoiceBlob(segment.blob, {
              ...transcriptionDependencies,
              durationMs: segment.durationMs,
              attemptCount: segment.attemptCount,
            });
      } catch (error) {
        segment.error = error;
      }
      segment = queue.shift();
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(concurrency, queue.length) }, () => worker()),
  );
};

export const voiceTranscriptFromSegments = (
  segments: VoiceTranscriptionSegment[],
): string => {
  const failed = segments.find((segment) => segment.error || !segment.text);
  if (failed) {
    if (failed.error instanceof Error) throw failed.error;
    throw new VoiceTranscriptionError('failed', { cause: failed.error });
  }
  const text = segments.map((segment) => segment.text).join(' ').trim();
  if (!text) throw new VoiceTranscriptionError('empty');
  return text;
};
