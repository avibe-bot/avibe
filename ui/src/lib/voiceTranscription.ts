import { apiFetch } from './apiFetch';
import { avibeFetch, CloudUnavailableError } from './avibeFetch';

export const MAX_VOICE_RECORDING_MS = 290_000;
export const VOICE_AUDIO_BITS_PER_SECOND = 32_000;

const TRANSCRIPTION_TIMEOUT_MS = 65_000;

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

type VoiceTranscriptionDependencies = {
  cloudFetch?: VoiceFetch;
  localFetch?: VoiceFetch;
  timeoutMs?: number;
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

const requestTimeout = (durationMs: number) => {
  const controller = new AbortController();
  const timer = globalThis.setTimeout(
    () => controller.abort(new DOMException('transcription timed out', 'TimeoutError')),
    durationMs,
  );
  return {
    signal: controller.signal,
    cancel: () => globalThis.clearTimeout(timer),
  };
};

const isTimeoutError = (error: unknown): boolean =>
  error instanceof DOMException && (error.name === 'AbortError' || error.name === 'TimeoutError');

const responseError = async (response: Response): Promise<VoiceTranscriptionError> => {
  const payload = (await response.json().catch(() => null)) as { error?: unknown } | null;
  const upstreamCode = typeof payload?.error === 'string' ? payload.error : '';
  if (response.status === 413 || upstreamCode === 'file_too_large') {
    return new VoiceTranscriptionError('too_large', { status: response.status });
  }
  if (response.status === 504 || upstreamCode === 'transcription_timeout') {
    return new VoiceTranscriptionError('timeout', { status: response.status });
  }
  if (response.status === 503 || upstreamCode === 'asr_not_configured') {
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
  timeoutMs: number,
): Promise<string> => {
  const timeout = requestTimeout(timeoutMs);
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
      signal: timeout.signal,
    });
    return await responseText(response);
  } catch (error) {
    if (error instanceof VoiceTranscriptionError) throw error;
    if (isTimeoutError(error) || timeout.signal.aborted) {
      throw new VoiceTranscriptionError('timeout', { cause: error });
    }
    throw new VoiceTranscriptionError('failed', { cause: error });
  } finally {
    timeout.cancel();
  }
};

export const transcribeVoiceBlob = async (
  blob: Blob,
  dependencies: VoiceTranscriptionDependencies = {},
): Promise<string> => {
  const cloudFetch = dependencies.cloudFetch ?? avibeFetch;
  const localFetch = dependencies.localFetch ?? apiFetch;
  const timeoutMs = dependencies.timeoutMs ?? TRANSCRIPTION_TIMEOUT_MS;
  const timeout = requestTimeout(timeoutMs);
  try {
    const form = new FormData();
    form.set('file', blob, voiceRecordingFileName(blob));
    const response = await cloudFetch('/api/cloud/audio/transcriptions', {
      method: 'POST',
      body: form,
      signal: timeout.signal,
    });
    return await responseText(response);
  } catch (error) {
    if (error instanceof CloudUnavailableError) {
      return transcribeLocally(blob, localFetch, timeoutMs);
    }
    if (error instanceof VoiceTranscriptionError) throw error;
    if (isTimeoutError(error) || timeout.signal.aborted) {
      throw new VoiceTranscriptionError('timeout', { cause: error });
    }
    throw new VoiceTranscriptionError('failed', { cause: error });
  } finally {
    timeout.cancel();
  }
};
