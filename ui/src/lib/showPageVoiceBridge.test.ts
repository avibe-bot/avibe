import { describe, expect, it, vi } from 'vitest';

import {
  ANNOTATION_VOICE_EVENT_MESSAGE,
  ANNOTATION_VOICE_REQUEST_MESSAGE,
  ShowPageVoiceHost,
  showPageVoiceRequestFromPayload,
  type ShowPageVoiceEvent,
  type ShowPageVoiceSession,
} from './showPageVoiceBridge';

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
};

const deferred = <T>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
};

class FakeVoiceSession implements ShowPageVoiceSession {
  readonly result = deferred<string>();
  readonly retryResult = deferred<string>();
  readonly done = this.result.promise;
  readonly start = vi.fn(async () => undefined);
  readonly finish = vi.fn();
  readonly canRetry = vi.fn(() => true);
  readonly retry = vi.fn((_context: { before: string; after: string }) => this.retryResult.promise);
  readonly abort = vi.fn();
}

describe('Show Page voice request boundary', () => {
  it('accepts the protocol and rejects oversized context or identifiers', () => {
    expect(showPageVoiceRequestFromPayload({
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'start',
      requestId: 'voice-1',
      before: 'before',
      after: 'after',
    })).toMatchObject({ action: 'start', requestId: 'voice-1' });
    expect(showPageVoiceRequestFromPayload({
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'start',
      requestId: 'voice-1',
      before: 'x'.repeat(501),
      after: '',
    })).toBeUndefined();
    expect(showPageVoiceRequestFromPayload({
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'query',
      requestId: 'x'.repeat(129),
    })).toBeUndefined();
  });
});

describe('ShowPageVoiceHost', () => {
  it('reports availability without starting a microphone session', async () => {
    const events: ShowPageVoiceEvent[] = [];
    const createSession = vi.fn();
    const host = new ShowPageVoiceHost({
      post: (event) => events.push(event),
      availability: async () => ({ available: true, maxFileBytes: null }),
      createSession,
    });

    host.handle({
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'query',
      requestId: 'probe-1',
    });
    await vi.waitFor(() => expect(events).toContainEqual({
      type: ANNOTATION_VOICE_EVENT_MESSAGE,
      kind: 'availability',
      requestId: 'probe-1',
      available: true,
    }));
    expect(createSession).not.toHaveBeenCalled();
  });

  it('owns start, preview, stop, and result for one iframe session', async () => {
    const events: ShowPageVoiceEvent[] = [];
    const session = new FakeVoiceSession();
    let preview: ((text: string) => void) | undefined;
    const host = new ShowPageVoiceHost({
      post: (event) => events.push(event),
      availability: async () => ({ available: true, maxFileBytes: 1_000_000 }),
      createSession: (input) => {
        preview = input.onPreview;
        expect(input).toMatchObject({
          before: 'before',
          after: 'after',
          maxFileBytes: 1_000_000,
        });
        return session;
      },
    });
    const request = {
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'start',
      requestId: 'voice-1',
      before: 'before',
      after: 'after',
    } as const;

    host.handle(request);
    await vi.waitFor(() => expect(events).toContainEqual({
      type: ANNOTATION_VOICE_EVENT_MESSAGE,
      kind: 'started',
      requestId: 'voice-1',
    }));
    preview?.('live words');
    expect(events.at(-1)).toEqual({
      type: ANNOTATION_VOICE_EVENT_MESSAGE,
      kind: 'preview',
      requestId: 'voice-1',
      text: 'live words',
    });

    host.handle({ ...request, action: 'stop' });
    expect(session.finish).toHaveBeenCalledOnce();
    session.result.resolve('final words');
    await vi.waitFor(() => expect(events.at(-1)).toEqual({
      type: ANNOTATION_VOICE_EVENT_MESSAGE,
      kind: 'result',
      requestId: 'voice-1',
      text: 'final words',
    }));
  });

  it('cancels microphone startup while availability is still pending', async () => {
    const events: ShowPageVoiceEvent[] = [];
    const availability = deferred<{ available: boolean; maxFileBytes: null }>();
    const createSession = vi.fn(() => new FakeVoiceSession());
    const host = new ShowPageVoiceHost({
      post: (event) => events.push(event),
      availability: () => availability.promise,
      createSession,
    });
    const start = {
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'start',
      requestId: 'voice-1',
      before: '',
      after: '',
    } as const;

    host.handle(start);
    host.handle({ ...start, requestId: 'voice-2' });
    expect(events.at(-1)).toMatchObject({
      kind: 'error',
      requestId: 'voice-2',
      code: 'failed',
      retryable: false,
    });

    host.handle({ ...start, action: 'abort' });
    availability.resolve({ available: true, maxFileBytes: null });
    await availability.promise;
    await Promise.resolve();

    expect(createSession).not.toHaveBeenCalled();
  });

  it('retains a failed transcription for retry and aborts on disposal', async () => {
    const events: ShowPageVoiceEvent[] = [];
    const session = new FakeVoiceSession();
    const host = new ShowPageVoiceHost({
      post: (event) => events.push(event),
      availability: async () => ({ available: true, maxFileBytes: null }),
      createSession: () => session,
    });
    const start = {
      type: ANNOTATION_VOICE_REQUEST_MESSAGE,
      action: 'start',
      requestId: 'voice-1',
      before: '',
      after: '',
    } as const;
    host.handle(start);
    await vi.waitFor(() => expect(session.start).toHaveBeenCalledOnce());
    session.result.reject(new Error('network'));
    await vi.waitFor(() => expect(events.at(-1)).toMatchObject({
      kind: 'error',
      requestId: 'voice-1',
      code: 'failed',
      retryable: true,
    }));

    host.handle({ ...start, action: 'retry', before: 'latest before', after: 'latest after' });
    expect(session.retry).toHaveBeenCalledWith({
      before: 'latest before',
      after: 'latest after',
    });
    session.retryResult.resolve('recovered');
    await vi.waitFor(() => expect(events.at(-1)).toMatchObject({
      kind: 'result',
      text: 'recovered',
    }));

    const replacement = new FakeVoiceSession();
    const nextHost = new ShowPageVoiceHost({
      post: () => undefined,
      availability: async () => ({ available: true, maxFileBytes: null }),
      createSession: () => replacement,
    });
    nextHost.handle({ ...start, requestId: 'voice-2' });
    await vi.waitFor(() => expect(replacement.start).toHaveBeenCalledOnce());
    nextHost.dispose();
    expect(replacement.abort).toHaveBeenCalledOnce();
  });
});
