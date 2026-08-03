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

const ASR_UPSTREAM_TIMEOUT_MS = 120_000;
const CLEANUP_UPSTREAM_TIMEOUT_MS = 30_000;
const SERVER_FINALIZATION_ALLOWANCE_MS = 5_000;
const COMPATIBILITY_UPSTREAM_TIMEOUT_MS = (
  ASR_UPSTREAM_TIMEOUT_MS
  + CLEANUP_UPSTREAM_TIMEOUT_MS
  + SERVER_FINALIZATION_ALLOWANCE_MS
);
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

export type VoiceCleanupOutcome = 'success' | 'fallback';

export type VoiceDictationResult = {
  text: string;
  cleanup: VoiceCleanupOutcome;
};

export type VoiceTranscriptionSegment = {
  blob: Blob | null;
  sequence: number;
  final: boolean;
  durationMs?: number;
  overlapMs?: number;
  attemptCount?: number;
  receipt?: string;
  error?: unknown;
};

type VoiceSegmentTranscriptionDependencies = VoiceTranscriptionDependencies & {
  transcribe?: (blob: Blob) => Promise<string>;
};

type VoiceFinalizationDependencies = VoiceTranscriptionDependencies & {
  finalize?: (input: {
    blob: Blob | null;
    sequence: number;
    receipts: string[];
  }) => Promise<VoiceDictationResult>;
};

type VoiceRequestMetadata = {
  dictationId: string;
  sequence: number;
  overlapMs: number;
  final: boolean;
  finalizeOnly: boolean;
  receipts: string[];
  before: string;
  after: string;
};

type VoiceServerResponse =
  | { kind: 'receipt'; receipt: string; sequence: number }
  | { kind: 'text'; text: string; cleanup?: VoiceCleanupOutcome };

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

const responsePayload = async (response: Response): Promise<VoiceServerResponse> => {
  if (!response.ok) throw await responseError(response);
  const payload = await response.json().catch(() => null) as unknown;
  if (
    payload == null
    || typeof payload !== 'object'
    || Array.isArray(payload)
  ) {
    throw new VoiceTranscriptionError('failed', { status: response.status });
  }
  const body = payload as { text?: unknown; cleanup?: unknown; receipt?: unknown; sequence?: unknown };
  if (
    typeof body.receipt === 'string'
    && body.receipt.length > 0
    && Number.isInteger(body.sequence)
    && (body.sequence as number) >= 0
  ) {
    return { kind: 'receipt', receipt: body.receipt, sequence: body.sequence as number };
  }
  if (typeof body.text === 'string') {
    if (!body.text.trim()) {
      throw new VoiceTranscriptionError('empty', { status: response.status });
    }
    const cleanup = body.cleanup === 'success' || body.cleanup === 'fallback'
      ? body.cleanup
      : undefined;
    return { kind: 'text', text: body.text, cleanup };
  }
  throw new VoiceTranscriptionError('failed', { status: response.status });
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
  blob: Blob | null,
  localFetch: VoiceFetch,
  signal: AbortSignal,
  metadata?: VoiceRequestMetadata,
): Promise<VoiceServerResponse> => {
  try {
    const data = blob ? await readBlobAsBase64(blob) : undefined;
    const response = await localFetch('/api/asr/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...(blob
          ? {
              name: voiceRecordingFileName(blob),
              mime: normalizedMimeType(blob),
            }
          : {}),
        data,
        ...(metadata
          ? {
              dictation_id: metadata.dictationId,
              sequence: metadata.sequence,
              overlap_ms: metadata.overlapMs,
              final: metadata.final,
              finalize_only: metadata.finalizeOnly,
              receipts: metadata.receipts,
              before: metadata.before,
              after: metadata.after,
            }
          : {}),
      }),
      signal,
    });
    return await responsePayload(response);
  } catch (error) {
    throw normalizeTranscriptionError(error, signal);
  }
};

const requestVoiceServer = async (
  blob: Blob | null,
  dependencies: VoiceTranscriptionDependencies = {},
  metadata?: VoiceRequestMetadata,
): Promise<VoiceServerResponse> => {
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
        sizeBytes: blob?.size ?? 0,
        mimeType: blob ? normalizedMimeType(blob) : undefined,
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
    if (blob) form.set('file', blob, voiceRecordingFileName(blob));
    if (metadata) {
      form.set('dictation_id', metadata.dictationId);
      form.set('sequence', String(metadata.sequence));
      form.set('overlap_ms', String(metadata.overlapMs));
      form.set('final', String(metadata.final));
      if (metadata.finalizeOnly) form.set('finalize_only', 'true');
      for (const receipt of metadata.receipts) form.append('receipt', receipt);
      if (metadata.before) form.set('before', metadata.before);
      if (metadata.after) form.set('after', metadata.after);
    }
    const response = await cloudFetch(
      metadata ? '/api/cloud/voice/dictations' : '/api/cloud/audio/transcriptions',
      {
        method: 'POST',
        body: form,
        signal: timeout.signal,
        onAttempt: handleCloudAttempt,
      },
    );
    const payload = await responsePayload(response);
    report(
      'cloud',
      'response',
      'success',
      cloudStageStartedAt,
      undefined,
      { attemptCount: cloudAttemptCount },
    );
    return payload;
  } catch (error) {
    if (error instanceof CloudUnavailableError && !error.uploadStarted) {
      report('cloud', 'token', 'fallback', cloudStageStartedAt);
      const localStartedAt = Date.now();
      try {
        const payload = await transcribeLocally(blob, localFetch, timeout.signal, metadata);
        report('local', 'response', 'success', localStartedAt);
        return payload;
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

export const transcribeVoiceBlob = async (
  blob: Blob,
  dependencies: VoiceTranscriptionDependencies = {},
): Promise<string> => {
  const response = await requestVoiceServer(blob, dependencies);
  if (response.kind !== 'text') throw new VoiceTranscriptionError('failed');
  if (!response.text.trim()) throw new VoiceTranscriptionError('empty');
  return response.text;
};

const transcribeVoiceSegment = async (
  segment: VoiceTranscriptionSegment,
  dependencies: VoiceSegmentTranscriptionDependencies,
): Promise<void> => {
  if (segment.final || !segment.blob) return;
  const { transcribe, ...transcriptionDependencies } = dependencies;
  segment.error = undefined;
  segment.attemptCount = (segment.attemptCount ?? 0) + 1;
  try {
    if (transcribe) {
      segment.receipt = await transcribe(segment.blob);
      return;
    }
    const dictationId = transcriptionDependencies.dictationId;
    if (!dictationId) throw new VoiceTranscriptionError('failed');
    const response = await requestVoiceServer(
      segment.blob,
      {
        ...transcriptionDependencies,
        durationMs: segment.durationMs,
        attemptCount: segment.attemptCount,
      },
      {
        dictationId,
        sequence: segment.sequence,
        overlapMs: segment.overlapMs ?? 0,
        final: false,
        finalizeOnly: false,
        receipts: [],
        before: '',
        after: '',
      },
    );
    if (response.kind !== 'receipt' || response.sequence !== segment.sequence) {
      throw new VoiceTranscriptionError('failed');
    }
    segment.receipt = response.receipt;
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
    if (segment.final) return Promise.resolve();
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
    !segment.final && !segment.receipt
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

export const finalizeVoiceDictation = async (
  segments: VoiceTranscriptionSegment[],
  input: {
    dictationId: string;
    before: string;
    after: string;
  },
  dependencies: VoiceFinalizationDependencies = {},
): Promise<VoiceDictationResult> => {
  const ordered = [...segments].sort((left, right) => left.sequence - right.sequence);
  const finalSegment = ordered.find((segment) => segment.final);
  if (!finalSegment || ordered.at(-1) !== finalSegment) {
    throw new VoiceTranscriptionError('failed');
  }
  const prior = ordered.filter((segment) => !segment.final);
  const failed = prior.find((segment) => segment.error || !segment.receipt);
  if (failed) {
    if (failed.error instanceof Error) throw failed.error;
    throw new VoiceTranscriptionError('failed', { cause: failed.error });
  }
  const receipts = prior.map((segment) => segment.receipt as string);
  finalSegment.error = undefined;
  finalSegment.attemptCount = (finalSegment.attemptCount ?? 0) + 1;
  try {
    const result = dependencies.finalize
      ? await dependencies.finalize({
          blob: finalSegment.blob,
          sequence: finalSegment.sequence,
          receipts,
        })
      : await (async () => {
          const response = await requestVoiceServer(
            finalSegment.blob,
            {
              ...dependencies,
              durationMs: finalSegment.durationMs,
              attemptCount: finalSegment.attemptCount,
              dictationId: input.dictationId,
            },
            {
              dictationId: input.dictationId,
              sequence: finalSegment.sequence,
              overlapMs: finalSegment.overlapMs ?? 0,
              final: true,
              finalizeOnly: finalSegment.blob === null,
              receipts,
              before: input.before,
              after: input.after,
            },
          );
          if (response.kind !== 'text' || !response.cleanup) {
            throw new VoiceTranscriptionError('failed');
          }
          return { text: response.text, cleanup: response.cleanup };
        })();
    if (!result.text.trim()) throw new VoiceTranscriptionError('empty');
    return result;
  } catch (error) {
    if (error instanceof VoiceTranscriptionError && error.status === 422) {
      for (const segment of prior) segment.receipt = undefined;
    }
    finalSegment.error = error;
    throw error;
  }
};
