import { apiFetch } from './apiFetch';
import {
  avibeFetch,
  CLOUD_TOKEN_MINT_TIMEOUT_MS,
  CloudUnavailableError,
  type AvibeFetchAttemptEvent,
  type AvibeFetchRequestInit,
} from './avibeFetch';
import { resolveVoiceContext, type VoiceContext, type VoiceContextSource } from './voiceContext';
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

// One dictation request's own deadline, sized to what it carries: a fixed allowance for the token,
// the connection, and recognition, the upload at a slow but working uplink's pace, and the final
// request's cleanup on top. A request on a dead path ends here instead of whenever the platform's
// network stack gives up (iOS takes about 13 s), and the retries below take over.
const DICTATION_ATTEMPT_BASE_MS = 15_000;
const DICTATION_UPLOAD_BYTES_PER_SECOND = 128 * 1024;
const DICTATION_FINAL_ALLOWANCE_MS = CLEANUP_UPSTREAM_TIMEOUT_MS + SERVER_FINALIZATION_ALLOWANCE_MS;

export const voiceDictationAttemptTimeoutMs = (bytes: number, final: boolean): number => Math.min(
  VOICE_TRANSCRIPTION_TIMEOUT_MS,
  DICTATION_ATTEMPT_BASE_MS
    + Math.ceil((Math.max(0, bytes) * 1000) / DICTATION_UPLOAD_BYTES_PER_SECOND)
    + (final ? DICTATION_FINAL_ALLOWANCE_MS : 0),
);

/** The pauses between automatic attempts of one dictation request: three attempts in all. */
export const VOICE_RETRY_DELAYS_MS: readonly number[] = [1_000, 3_000];
/** After a release, no new automatic attempt starts this long after the first one: the take is
 *  then left for the user to retry, its audio kept, instead of 「正在整理」 waiting on a network
 *  that is not there. */
export const VOICE_SETTLE_RETRY_WINDOW_MS = 10_000;

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
  /** The conversation this dictation is spoken into, sent with every segment (each is recognized
   *  on its own). Pass one `resolveVoiceContext` promise per dictation, so it settles once. */
  context?: VoiceContextSource;
  /** Pauses between automatic attempts of one dictation request (`VOICE_RETRY_DELAYS_MS`). */
  retryDelaysMs?: readonly number[];
  /** No automatic retry starts after this time (epoch ms); the request in flight still finishes. */
  retryUntil?: number;
  /** Aborted, no further automatic retry starts; unlike `signal`, the request in flight goes on. */
  retrySignal?: AbortSignal;
  /** Whether the browser reports a network at all (`navigator.onLine`). */
  online?: () => boolean;
};

// avibe.bot declares this on the user token when its dictation segments accept `reply` and
// `user_previous`. The local relay forwards them as given: the same release built this page and
// the relay, and avibe.bot reads a segment's form by field name, so an older one ignores them.
const DICTATION_CONTEXT_CAPABILITY = 'dictation_context';
// And this when they accept `vocabulary` (the names to hear exactly), repeated.
const DICTATION_VOCABULARY_CAPABILITY = 'dictation_vocabulary';

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
  /** Sends a segment that has no receipt yet in place of the server request (tests). */
  transcribe?: (blob: Blob) => Promise<string>;
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
  context?: VoiceContext;
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
  // fetch rejects with a TypeError when the request never got an answer: the network failed.
  if (error instanceof TypeError) {
    return new VoiceTranscriptionError('unavailable', { cause: error });
  }
  return new VoiceTranscriptionError('failed', { cause: error });
};

const telemetryOutcome = (error: VoiceTranscriptionError): VoiceTelemetryOutcome => error.code;

const browserOnline = (): boolean => (
  typeof navigator === 'undefined' || navigator.onLine !== false
);

/** Whether trying the same request again can change its answer: the network or the service
 *  failed, not the request itself. An empty, oversized, cancelled, or rejected request cannot. */
export const isRetryableVoiceError = (error: unknown): boolean => {
  if (!(error instanceof VoiceTranscriptionError)) return false;
  if (error.code === 'unavailable' || error.code === 'timeout') return true;
  if (error.code !== 'failed') return false;
  return error.status == null || error.status >= 500 || error.status === 408 || error.status === 429;
};

const anySignal = (...signals: Array<AbortSignal | undefined>): AbortSignal | undefined => {
  const present = signals.filter((signal): signal is AbortSignal => signal !== undefined);
  if (present.length <= 1) return present[0];
  const controller = new AbortController();
  for (const signal of present) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', () => controller.abort(), { once: true });
  }
  return controller.signal;
};

/** Waits `ms`, or resolves false at once when `signal` aborts. */
const pause = (ms: number, signal?: AbortSignal): Promise<boolean> => new Promise((resolve) => {
  if (signal?.aborted) {
    resolve(false);
    return;
  }
  const onAbort = () => {
    globalThis.clearTimeout(timer);
    resolve(false);
  };
  const timer = globalThis.setTimeout(() => {
    signal?.removeEventListener('abort', onAbort);
    resolve(true);
  }, ms);
  signal?.addEventListener('abort', onAbort, { once: true });
});

/**
 * Runs one dictation request, again after a pause while its failure is the network's or the
 * service's (`isRetryableVoiceError`), at most `retryDelaysMs.length + 1` times. A browser with no
 * network fails at once rather than waiting out a request that cannot leave; it is retried when
 * the network returns, by the caller. No attempt starts after `retryUntil`.
 */
const withVoiceRetries = async <T>(
  dependencies: VoiceTranscriptionDependencies,
  attempt: () => Promise<T>,
): Promise<T> => {
  const delays = dependencies.retryDelaysMs ?? VOICE_RETRY_DELAYS_MS;
  const online = dependencies.online ?? browserOnline;
  for (let index = 0; ; index += 1) {
    if (!online()) throw new VoiceTranscriptionError('unavailable');
    try {
      return await attempt();
    } catch (error) {
      const delay = delays[index];
      if (
        delay === undefined
        || !isRetryableVoiceError(error)
        || dependencies.signal?.aborted
        || dependencies.retrySignal?.aborted
        || !online()
        || (dependencies.retryUntil !== undefined && Date.now() + delay > dependencies.retryUntil)
      ) {
        throw error;
      }
      if (!(await pause(delay, anySignal(dependencies.signal, dependencies.retrySignal)))) throw error;
      if (dependencies.retrySignal?.aborted) throw error;
    }
  }
};

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
              ...(metadata.context?.reply ? { reply: metadata.context.reply } : {}),
              ...(metadata.context?.userPrevious ? { user_previous: metadata.context.userPrevious } : {}),
              ...(metadata.context?.vocabulary?.length ? { vocabulary: metadata.context.vocabulary } : {}),
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
  const timeoutMs = dependencies.timeoutMs ?? (
    metadata
      ? voiceDictationAttemptTimeoutMs(blob?.size ?? 0, metadata.final)
      : VOICE_TRANSCRIPTION_TIMEOUT_MS
  );
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
  // Built for the token each attempt carries: only one whose avibe.bot declares
  // `dictation_context` gets the conversation.
  const formFor = (capabilities: readonly string[]): FormData => {
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
      if (capabilities.includes(DICTATION_CONTEXT_CAPABILITY)) {
        if (metadata.context?.reply) form.set('reply', metadata.context.reply);
        if (metadata.context?.userPrevious) form.set('user_previous', metadata.context.userPrevious);
      }
      if (capabilities.includes(DICTATION_VOCABULARY_CAPABILITY)) {
        for (const word of metadata.context?.vocabulary ?? []) form.append('vocabulary', word);
      }
    }
    return form;
  };
  try {
    const response = await cloudFetch(
      metadata ? '/api/cloud/voice/dictations' : '/api/cloud/audio/transcriptions',
      {
        method: 'POST',
        bodyFor: formFor,
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
    // A dictation request the cloud could not take (no token, a refused token that could not be
    // replaced, or a network failure before any answer) goes through the local relay, a different
    // network path. Repeating a dictation request is safe: segments are recognized statelessly
    // and the final one only joins receipts. A timeout or an answer from the cloud is final here.
    const cloudNetworkFailure = metadata !== undefined
      && !timeout.signal.aborted
      && (error instanceof CloudUnavailableError || error instanceof TypeError);
    if ((error instanceof CloudUnavailableError && !error.uploadStarted) || cloudNetworkFailure) {
      report(
        'cloud',
        error instanceof CloudUnavailableError && !error.uploadStarted
          ? 'token'
          : error instanceof CloudUnavailableError ? 'refresh' : 'upload',
        'fallback',
        cloudStageStartedAt,
        undefined,
        { attemptCount: cloudAttemptCount },
      );
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
  const blob = segment.blob;
  const { transcribe, ...transcriptionDependencies } = dependencies;
  // Sending a segment that already failed again is a retry: none starts after the take's
  // window, however long the requests before it took. A segment never sent still gets its turn.
  if (
    segment.error !== undefined
    && transcriptionDependencies.retryUntil !== undefined
    && Date.now() >= transcriptionDependencies.retryUntil
  ) {
    return;
  }
  segment.error = undefined;
  try {
    await withVoiceRetries(transcriptionDependencies, async () => {
      segment.attemptCount = (segment.attemptCount ?? 0) + 1;
      if (transcribe) {
        segment.receipt = await transcribe(blob);
        return;
      }
      const dictationId = transcriptionDependencies.dictationId;
      if (!dictationId) throw new VoiceTranscriptionError('failed');
      const response = await requestVoiceServer(
        blob,
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
          context: await resolveVoiceContext(transcriptionDependencies.context),
        },
      );
      if (response.kind !== 'receipt' || response.sequence !== segment.sequence) {
        throw new VoiceTranscriptionError('failed');
      }
      segment.receipt = response.receipt;
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
  // A segment that has no receipt yet (it failed, or was never sent) is sent again now, with its
  // retries: one failed minute does not void the take, and the last segment is still sent.
  const { finalize, transcribe, ...requestDependencies } = dependencies;
  if (prior.some((segment) => !segment.receipt)) {
    await transcribeVoiceSegments(prior, { ...requestDependencies, dictationId: input.dictationId, transcribe });
  }
  const failed = prior.find((segment) => segment.error || !segment.receipt);
  if (failed) {
    if (failed.error instanceof Error) throw failed.error;
    throw new VoiceTranscriptionError('failed', { cause: failed.error });
  }
  const receipts = prior.map((segment) => segment.receipt as string);
  finalSegment.error = undefined;
  try {
    const result = await withVoiceRetries(requestDependencies, async () => {
      finalSegment.attemptCount = (finalSegment.attemptCount ?? 0) + 1;
      if (finalize) {
        return finalize({
          blob: finalSegment.blob,
          sequence: finalSegment.sequence,
          receipts,
        });
      }
      const response = await requestVoiceServer(
        finalSegment.blob,
        {
          ...requestDependencies,
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
          context: await resolveVoiceContext(requestDependencies.context),
        },
      );
      if (response.kind !== 'text' || !response.cleanup) {
        throw new VoiceTranscriptionError('failed');
      }
      return { text: response.text, cleanup: response.cleanup };
    });
    if (!result.text.trim()) throw new VoiceTranscriptionError('empty');
    return result;
  } catch (error) {
    // Receipts the server refused, or ones that joined into nothing: a retry recognizes the
    // retained audio again instead of finalizing the same receipts.
    if (error instanceof VoiceTranscriptionError && (error.status === 422 || error.code === 'empty')) {
      for (const segment of prior) segment.receipt = undefined;
    }
    finalSegment.error = error;
    throw error;
  }
};
