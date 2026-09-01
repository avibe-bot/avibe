import {
  ShowPageVoiceDictation,
  type ShowPageVoiceDictationInput,
} from './showPageVoiceDictation';
import {
  VoiceTranscriptionError,
  type VoiceTranscriptionErrorCode,
} from './voiceTranscription';

export const ANNOTATION_VOICE_REQUEST_MESSAGE = 'avibe:annotation:voice:request';
export const ANNOTATION_VOICE_EVENT_MESSAGE = 'avibe:annotation:voice:event';
const MAX_REQUEST_ID_CHARS = 128;
const MAX_CONTEXT_BEFORE_CHARS = 500;
const MAX_CONTEXT_AFTER_CHARS = 200;

export type ShowPageVoiceErrorCode = VoiceTranscriptionErrorCode | 'permission' | 'start_failed';

export type ShowPageVoiceRequest =
  | { type: typeof ANNOTATION_VOICE_REQUEST_MESSAGE; action: 'query'; requestId: string }
  | {
      type: typeof ANNOTATION_VOICE_REQUEST_MESSAGE;
      action: 'start';
      requestId: string;
      before: string;
      after: string;
    }
  | {
      type: typeof ANNOTATION_VOICE_REQUEST_MESSAGE;
      action: 'stop' | 'retry' | 'abort';
      requestId: string;
    };

export type ShowPageVoiceEvent =
  | {
      type: typeof ANNOTATION_VOICE_EVENT_MESSAGE;
      kind: 'availability';
      requestId: string;
      available: boolean;
    }
  | {
      type: typeof ANNOTATION_VOICE_EVENT_MESSAGE;
      kind: 'started' | 'preview' | 'result';
      requestId: string;
      text?: string;
    }
  | {
      type: typeof ANNOTATION_VOICE_EVENT_MESSAGE;
      kind: 'error';
      requestId: string;
      code: ShowPageVoiceErrorCode;
      retryable: boolean;
    };

export type ShowPageVoiceAvailability = {
  available: boolean;
  maxFileBytes: number | null;
};

export type ShowPageVoiceSession = {
  readonly done: Promise<string>;
  start(): Promise<void>;
  finish(): void;
  canRetry(): boolean;
  retry(): Promise<string>;
  abort(): void;
};

export type ShowPageVoiceHostDependencies = {
  post: (event: ShowPageVoiceEvent) => void;
  availability: () => Promise<ShowPageVoiceAvailability>;
  createSession?: (input: ShowPageVoiceDictationInput) => ShowPageVoiceSession;
};

const boundedString = (value: unknown, maxChars: number): string | undefined => (
  typeof value === 'string' && value.length <= maxChars ? value : undefined
);

export function showPageVoiceRequestFromPayload(value: unknown): ShowPageVoiceRequest | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const payload = value as Record<string, unknown>;
  if (payload.type !== ANNOTATION_VOICE_REQUEST_MESSAGE) return undefined;
  const requestId = boundedString(payload.requestId, MAX_REQUEST_ID_CHARS);
  if (!requestId) return undefined;
  if (payload.action === 'query') {
    return { type: ANNOTATION_VOICE_REQUEST_MESSAGE, action: 'query', requestId };
  }
  if (payload.action === 'start') {
    const before = boundedString(payload.before, MAX_CONTEXT_BEFORE_CHARS);
    const after = boundedString(payload.after, MAX_CONTEXT_AFTER_CHARS);
    if (before === undefined || after === undefined) return undefined;
    return {
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'start',
      requestId,
      before,
      after,
    };
  }
  if (payload.action === 'stop' || payload.action === 'retry' || payload.action === 'abort') {
    return { type: ANNOTATION_VOICE_REQUEST_MESSAGE, action: payload.action, requestId };
  }
  return undefined;
}

const voiceErrorCode = (error: unknown, starting = false): ShowPageVoiceErrorCode => {
  if (error instanceof VoiceTranscriptionError) return error.code;
  const name = error && typeof error === 'object'
    ? (error as { name?: unknown }).name
    : undefined;
  if (name === 'NotAllowedError' || name === 'SecurityError') return 'permission';
  return starting ? 'start_failed' : 'failed';
};

/** One voice owner for one mounted Show Page iframe. */
export class ShowPageVoiceHost {
  private readonly dependencies: Required<ShowPageVoiceHostDependencies>;
  private active: { requestId: string; session: ShowPageVoiceSession } | null = null;
  private starting: { requestId: string; cancelled: boolean } | null = null;
  private disposed = false;

  constructor(dependencies: ShowPageVoiceHostDependencies) {
    this.dependencies = {
      ...dependencies,
      createSession: dependencies.createSession
        ?? ((input) => new ShowPageVoiceDictation(input)),
    };
  }

  handle(value: unknown): void {
    const request = showPageVoiceRequestFromPayload(value);
    if (!request || this.disposed) return;
    if (request.action === 'query') {
      void this.replyAvailability(request.requestId);
    } else if (request.action === 'start') {
      void this.start(request);
    } else if (request.action === 'stop') {
      if (this.active?.requestId === request.requestId) this.active.session.finish();
    } else if (request.action === 'retry') {
      if (this.active?.requestId === request.requestId) void this.retry(this.active);
    } else {
      if (this.starting?.requestId === request.requestId) {
        this.starting.cancelled = true;
        this.starting = null;
      }
      if (this.active?.requestId === request.requestId) {
        this.active.session.abort();
        this.active = null;
      }
    }
  }

  dispose(): void {
    this.disposed = true;
    if (this.starting) this.starting.cancelled = true;
    this.starting = null;
    this.active?.session.abort();
    this.active = null;
  }

  private async replyAvailability(requestId: string): Promise<void> {
    try {
      const availability = await this.dependencies.availability();
      if (!this.disposed) {
        this.dependencies.post({
          type: ANNOTATION_VOICE_EVENT_MESSAGE,
          kind: 'availability',
          requestId,
          available: availability.available,
        });
      }
    } catch {
      if (!this.disposed) {
        this.dependencies.post({
          type: ANNOTATION_VOICE_EVENT_MESSAGE,
          kind: 'availability',
          requestId,
          available: false,
        });
      }
    }
  }

  private async start(request: Extract<ShowPageVoiceRequest, { action: 'start' }>): Promise<void> {
    if (this.active || this.starting) {
      this.postError(request.requestId, 'failed', false);
      return;
    }
    const pending = { requestId: request.requestId, cancelled: false };
    this.starting = pending;
    try {
      const availability = await this.dependencies.availability();
      if (this.disposed || pending.cancelled || this.starting !== pending) return;
      if (!availability.available) {
        this.postError(request.requestId, 'unavailable', false);
        return;
      }
      const session = this.dependencies.createSession({
        before: request.before,
        after: request.after,
        maxFileBytes: availability.maxFileBytes,
        onPreview: (text) => {
          if (this.active?.requestId !== request.requestId || this.disposed) return;
          this.dependencies.post({
            type: ANNOTATION_VOICE_EVENT_MESSAGE,
            kind: 'preview',
            requestId: request.requestId,
            text,
          });
        },
      });
      const active = { requestId: request.requestId, session };
      this.starting = null;
      this.active = active;
      try {
        await session.start();
      } catch (error) {
        if (this.active === active) this.active = null;
        this.postError(request.requestId, voiceErrorCode(error, true), false);
        return;
      }
      if (this.disposed || this.active !== active) {
        session.abort();
        return;
      }
      this.dependencies.post({
        type: ANNOTATION_VOICE_EVENT_MESSAGE,
        kind: 'started',
        requestId: request.requestId,
      });
      void session.done.then(
        (text) => this.postResult(active, text),
        (error) => this.postSessionError(active, error),
      );
    } catch (error) {
      this.postError(request.requestId, voiceErrorCode(error, true), false);
    } finally {
      if (this.starting === pending) this.starting = null;
    }
  }

  private async retry(active: { requestId: string; session: ShowPageVoiceSession }): Promise<void> {
    try {
      const text = await active.session.retry();
      this.postResult(active, text);
    } catch (error) {
      this.postSessionError(active, error);
    }
  }

  private postResult(
    active: { requestId: string; session: ShowPageVoiceSession },
    text: string,
  ): void {
    if (this.disposed || this.active !== active) return;
    this.active = null;
    this.dependencies.post({
      type: ANNOTATION_VOICE_EVENT_MESSAGE,
      kind: 'result',
      requestId: active.requestId,
      text,
    });
  }

  private postSessionError(
    active: { requestId: string; session: ShowPageVoiceSession },
    error: unknown,
  ): void {
    if (this.disposed || this.active !== active) return;
    const code = voiceErrorCode(error);
    const retryable = code !== 'cancelled' && code !== 'empty' && active.session.canRetry();
    if (!retryable) this.active = null;
    this.postError(active.requestId, code, retryable);
  }

  private postError(requestId: string, code: ShowPageVoiceErrorCode, retryable: boolean): void {
    if (this.disposed) return;
    this.dependencies.post({
      type: ANNOTATION_VOICE_EVENT_MESSAGE,
      kind: 'error',
      requestId,
      code,
      retryable,
    });
  }
}
