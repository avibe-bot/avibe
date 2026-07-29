import { apiFetch } from './apiFetch';
import {
  avibeFetch,
  CLOUD_TOKEN_MINT_TIMEOUT_MS,
  CloudUnavailableError,
  type AvibeFetchAttemptEvent,
  type AvibeFetchRequestInit,
} from './avibeFetch';
import {
  emitVoiceTelemetry,
  type VoiceTelemetryEvent,
  type VoiceTelemetryOutcome,
} from './voiceTelemetry';

export const VOICE_SEGMENT_MS = 60_000;
export const VOICE_TRANSCRIPTION_CONCURRENCY = 2;

const COMPATIBILITY_UPSTREAM_TIMEOUT_MS = 120_000;
const COMPATIBILITY_UPLOAD_BUDGET_MS = 30_000;
const COMPATIBILITY_CSRF_ALLOWANCE_MS = 4_000;
export const VOICE_TRANSCRIPTION_TIMEOUT_MS = (
  CLOUD_TOKEN_MINT_TIMEOUT_MS
  + COMPATIBILITY_CSRF_ALLOWANCE_MS
  + COMPATIBILITY_UPLOAD_BUDGET_MS
  + COMPATIBILITY_UPSTREAM_TIMEOUT_MS
);

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

export type VoiceTranscriptionErrorCode =
  | 'cancelled'
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
type VoiceCloudFetch = (path: string, init?: AvibeFetchRequestInit) => Promise<Response>;

export type VoiceTranscriptionDependencies = {
  cloudFetch?: VoiceCloudFetch;
  localFetch?: VoiceFetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  durationMs?: number;
  attemptCount?: number;
  dictationId?: string;
  telemetry?: (event: VoiceTelemetryEvent) => void;
};

export type VoiceTranscriptionSegment = {
  blob: Blob;
  durationMs?: number;
  overlapMs?: number;
  attemptCount?: number;
  text?: string;
  error?: unknown;
};

const isEmptyVoiceSegment = (segment: VoiceTranscriptionSegment): boolean => (
  segment.error instanceof VoiceTranscriptionError
  && segment.error.code === 'empty'
  && !segment.text
);

type VoiceSegmentTranscriptionDependencies = VoiceTranscriptionDependencies & {
  transcribe?: (blob: Blob) => Promise<string>;
};

const normalizedMimeType = (blob: Blob): string =>
  blob.type.split(';', 1)[0]?.trim().toLowerCase() || 'audio/webm';

export const voiceRecordingFileName = (blob: Blob): string =>
  `voice.${EXTENSION_BY_MIME[normalizedMimeType(blob)] ?? 'webm'}`;

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

const isAbortError = (error: unknown): boolean =>
  error instanceof DOMException && error.name === 'AbortError';

const isTimeoutError = (error: unknown): boolean =>
  error instanceof DOMException && error.name === 'TimeoutError';

const normalizeTranscriptionError = (
  error: unknown,
  signal: AbortSignal,
): VoiceTranscriptionError => {
  if (error instanceof VoiceTranscriptionError) return error;
  if (isTimeoutError(error) || isTimeoutError(signal.reason)) {
    return new VoiceTranscriptionError('timeout', { cause: error });
  }
  if (isAbortError(error) || signal.aborted) {
    return new VoiceTranscriptionError('cancelled', { cause: error });
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
  if (upstreamCode === 'transcription_empty') {
    return new VoiceTranscriptionError('empty', { status: response.status });
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
  const payload = await response.json().catch(() => null) as unknown;
  if (
    payload == null
    || typeof payload !== 'object'
    || Array.isArray(payload)
    || typeof (payload as { text?: unknown }).text !== 'string'
  ) {
    throw new VoiceTranscriptionError('failed', { status: response.status });
  }
  const { text } = payload as { text: string };
  if (!text.trim()) throw new VoiceTranscriptionError('empty', { status: response.status });
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
  const timeoutMs = dependencies.timeoutMs ?? VOICE_TRANSCRIPTION_TIMEOUT_MS;
  const telemetry = dependencies.telemetry ?? emitVoiceTelemetry;
  const attemptCount = dependencies.attemptCount ?? 1;
  const timeout = requestTimeout(timeoutMs, dependencies.signal);
  let cloudAttemptCount = attemptCount;
  let cloudStageStartedAt = Date.now();
  const report = (
    path: 'cloud' | 'local',
    providerStage: NonNullable<VoiceTelemetryEvent['providerStage']>,
    outcome: VoiceTelemetryOutcome,
    startedAt: number,
    error?: VoiceTranscriptionError,
    overrides: {
      attemptCount?: number;
      elapsedMs?: number;
      httpStatus?: number;
    } = {},
  ) => {
    const reportedAttemptCount = overrides.attemptCount ?? attemptCount;
    try {
      telemetry({
        event: 'segment_transcription',
        path,
        providerStage,
        outcome,
        dictationId: dependencies.dictationId,
        sizeBytes: blob.size,
        mimeType: normalizedMimeType(blob),
        durationMs: dependencies.durationMs,
        elapsedMs: overrides.elapsedMs ?? Date.now() - startedAt,
        httpStatus: overrides.httpStatus ?? error?.status,
        attemptCount: reportedAttemptCount,
        retry: reportedAttemptCount > 1,
      });
    } catch {
      // Instrumentation cannot change transcription behavior.
    }
  };
  const handleCloudAttempt = (event: AvibeFetchAttemptEvent): void => {
    if (event.phase === 'started') {
      cloudAttemptCount = attemptCount + event.attempt - 1;
      cloudStageStartedAt = Date.now();
      return;
    }
    // Only the first 401 is hidden inside avibeFetch. The caller receives and
    // reports every terminal response, including a second 401 after refresh.
    if (event.attempt !== 1 || event.status !== 401) return;
    report(
      'cloud',
      'response',
      'failed',
      cloudStageStartedAt,
      undefined,
      {
        attemptCount: attemptCount + event.attempt - 1,
        elapsedMs: event.elapsedMs,
        httpStatus: event.status,
      },
    );
    // A refresh failure is a distinct stage, and a successful refresh will
    // replace this timestamp when the second HTTP attempt starts.
    cloudStageStartedAt = Date.now();
  };
  try {
    const form = new FormData();
    form.set('file', blob, voiceRecordingFileName(blob));
    const response = await cloudFetch('/api/cloud/audio/transcriptions', {
      method: 'POST',
      body: form,
      signal: timeout.signal,
      onAttempt: handleCloudAttempt,
    });
    const text = await responseText(response);
    report(
      'cloud',
      'response',
      'success',
      cloudStageStartedAt,
      undefined,
      { attemptCount: cloudAttemptCount },
    );
    return text;
  } catch (error) {
    if (error instanceof CloudUnavailableError && !error.uploadStarted) {
      report('cloud', 'token', 'fallback', cloudStageStartedAt);
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
    const normalized = error instanceof CloudUnavailableError
      || (error instanceof TypeError && !timeout.signal.aborted)
      ? new VoiceTranscriptionError('unavailable', { cause: error })
      : normalizeTranscriptionError(error, timeout.signal);
    const providerStage = error instanceof CloudUnavailableError
      ? 'refresh'
      : normalized.status == null
        ? 'upload'
        : 'response';
    report(
      'cloud',
      providerStage,
      telemetryOutcome(normalized),
      cloudStageStartedAt,
      normalized,
      { attemptCount: cloudAttemptCount },
    );
    throw normalized;
  } finally {
    timeout.cancel();
  }
};

const transcribeVoiceSegment = async (
  segment: VoiceTranscriptionSegment,
  dependencies: VoiceSegmentTranscriptionDependencies,
): Promise<void> => {
  const { transcribe, ...transcriptionDependencies } = dependencies;
  segment.error = undefined;
  segment.attemptCount = (segment.attemptCount ?? 0) + 1;
  try {
    segment.text = transcribe
      ? await transcribe(segment.blob)
      : await transcribeVoiceBlob(segment.blob, {
          ...transcriptionDependencies,
          durationMs: segment.durationMs,
          attemptCount: segment.attemptCount,
        });
  } catch (error) {
    segment.error = error;
  }
};

type VoiceTranscriptionQueueEntry = {
  segment: VoiceTranscriptionSegment;
  resolve: () => void;
};

export class VoiceTranscriptionQueue {
  private readonly concurrency: number;
  private readonly dependencies: VoiceSegmentTranscriptionDependencies;
  private readonly signal?: AbortSignal;
  private readonly pending: VoiceTranscriptionQueueEntry[] = [];
  private active = 0;

  constructor(
    dependencies: VoiceSegmentTranscriptionDependencies & {
      concurrency?: number;
    } = {},
  ) {
    const {
      concurrency: requestedConcurrency = VOICE_TRANSCRIPTION_CONCURRENCY,
      ...transcriptionDependencies
    } = dependencies;
    this.concurrency = Math.max(1, Math.floor(requestedConcurrency));
    this.dependencies = transcriptionDependencies;
    this.signal = transcriptionDependencies.signal;
    this.signal?.addEventListener('abort', this.discardPending, { once: true });
  }

  enqueue(segment: VoiceTranscriptionSegment): Promise<void> {
    const task = new Promise<void>((resolve) => {
      this.pending.push({ segment, resolve });
    });
    this.pump();
    return task;
  }

  private pump(): void {
    if (this.signal?.aborted) {
      this.discardPending();
      return;
    }
    while (this.active < this.concurrency) {
      const entry = this.pending.shift();
      if (!entry) return;
      this.active += 1;
      void this.run(entry);
    }
  }

  private readonly discardPending = (): void => {
    let entry = this.pending.shift();
    while (entry) {
      entry.resolve();
      entry = this.pending.shift();
    }
  };

  private async run(entry: VoiceTranscriptionQueueEntry): Promise<void> {
    try {
      await transcribeVoiceSegment(entry.segment, this.dependencies);
    } finally {
      this.active -= 1;
      entry.resolve();
      this.pump();
    }
  }
}

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
  const queue = segments.filter((segment) => (
    !segment.text && !isEmptyVoiceSegment(segment)
  ));
  const concurrency = Math.max(1, Math.floor(requestedConcurrency));
  const worker = async () => {
    let segment = queue.shift();
    while (segment) {
      if (transcriptionDependencies.signal?.aborted) return;
      await transcribeVoiceSegment(segment, {
        ...transcriptionDependencies,
        transcribe: customTranscribe,
      });
      segment = queue.shift();
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(concurrency, queue.length) }, () => worker()),
  );
};

const NO_SPACE_SCRIPT = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Thai}\p{Script=Lao}\p{Script=Khmer}\p{Script=Myanmar}]/u;
const WORD_CHARACTER = /[\p{L}\p{N}]/u;
const CLOSING_PUNCTUATION = /^[,.;:!?%)\]}，。；：！？）》】」』]/u;
const OPENING_PUNCTUATION = /[([{（《【「『]$/u;
const DOMAIN_SUFFIX = /^(?:\p{L}{2,63}|xn--[a-z0-9-]{1,59})(?:\.[\p{L}\p{N}](?:[\p{L}\p{N}-]{0,61}[\p{L}\p{N}])?)*(?:[/:?#]|$)/iu;
const EXPLICIT_URL_OR_EMAIL = /^(?:[a-z][a-z\d+.-]*:\/\/|www\.|[^@\s]+@)/iu;

const continuesDomain = (leftToken: string, rightToken: string): boolean => {
  if (!leftToken.endsWith('.')) return false;
  const suffix = rightToken.match(DOMAIN_SUFFIX)?.[0];
  if (!suffix) return false;
  return EXPLICIT_URL_OR_EMAIL.test(leftToken) || /[/:?#]$/u.test(suffix);
};

const voiceSegmentSeparator = (left: string, right: string): string => {
  const leftCharacter = left.at(-1) ?? '';
  const rightCharacter = right.at(0) ?? '';
  if (!leftCharacter || !rightCharacter || /\s/u.test(leftCharacter + rightCharacter)) return '';
  if (NO_SPACE_SCRIPT.test(leftCharacter) || NO_SPACE_SCRIPT.test(rightCharacter)) return '';
  if (CLOSING_PUNCTUATION.test(rightCharacter) || OPENING_PUNCTUATION.test(leftCharacter)) return '';

  const leftToken = left.match(/\S+$/u)?.[0] ?? '';
  const rightToken = right.match(/^\S+/u)?.[0] ?? '';
  if (
    /[-'@/_#=+%&?]$/u.test(leftToken)
    || /^[-'@/_#=+%&?]/u.test(rightToken)
    || (/^\p{N}+$/u.test(leftToken) && /^\p{N}+$/u.test(rightToken))
    || (
      /[\p{N}][.,:]$/u.test(leftToken)
      && /^\p{N}/u.test(rightToken)
    )
    || continuesDomain(leftToken, rightToken)
  ) {
    return '';
  }

  if (WORD_CHARACTER.test(rightCharacter)) return ' ';
  return '';
};

const trimTranscribedOverlap = (left: string, right: string): string => {
  const candidate = right.trimStart();
  const leftFolded = left.toLocaleLowerCase();
  const candidateFolded = candidate.toLocaleLowerCase();
  for (
    let length = Math.min(leftFolded.length, candidateFolded.length);
    length > 0;
    length -= 1
  ) {
    const overlap = candidateFolded.slice(0, length);
    if (!leftFolded.endsWith(overlap)) continue;

    const leftStart = left.length - length;
    const leftBefore = left.at(leftStart - 1) ?? '';
    const overlapFirst = candidate.at(0) ?? '';
    const overlapLast = candidate.at(length - 1) ?? '';
    const rightAfter = candidate.at(length) ?? '';
    const containsNoSpaceScript = NO_SPACE_SCRIPT.test(overlap);
    if (
      !containsNoSpaceScript
      && (
        (WORD_CHARACTER.test(leftBefore) && WORD_CHARACTER.test(overlapFirst))
        || (WORD_CHARACTER.test(overlapLast) && WORD_CHARACTER.test(rightAfter))
      )
    ) {
      continue;
    }
    return candidate.slice(length);
  }
  return right;
};

export const voiceTranscriptFromSegments = (
  segments: VoiceTranscriptionSegment[],
): string => {
  const failed = segments.find((segment) => (
    !isEmptyVoiceSegment(segment)
    && (segment.error || !segment.text)
  ));
  if (failed) {
    if (failed.error instanceof Error) throw failed.error;
    throw new VoiceTranscriptionError('failed', { cause: failed.error });
  }
  const transcribed = segments.filter((segment) => Boolean(segment.text));
  if (!transcribed.length) {
    const emptyError = segments.find(isEmptyVoiceSegment)?.error;
    if (emptyError instanceof Error) throw emptyError;
    throw new VoiceTranscriptionError('empty');
  }
  const text = transcribed.reduce((joined, segment) => {
    const rawPart = segment.text ?? '';
    const part = joined && (segment.overlapMs ?? 0) > 0
      ? trimTranscribedOverlap(joined, rawPart)
      : rawPart;
    if (!part) return joined;
    return joined ? `${joined}${voiceSegmentSeparator(joined, part)}${part}` : part;
  }, '').trim();
  if (!text) throw new VoiceTranscriptionError('empty');
  return text;
};
