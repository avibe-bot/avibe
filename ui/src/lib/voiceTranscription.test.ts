import { describe, expect, it, vi } from 'vitest';

import {
  CloudUnavailableError,
  CLOUD_TOKEN_MINT_TIMEOUT_MS,
  type AvibeFetchRequestInit,
} from './avibeFetch';
import {
  transcribeVoiceBlob,
  VoiceTranscriptionQueue,
  VoiceTranscriptionError,
  transcribeVoiceSegments,
  VOICE_TRANSCRIPTION_TIMEOUT_MS,
  voiceTranscriptFromSegments,
  voiceRecordingFileName,
} from './voiceTranscription';

const audioBlob = () => new Blob(['audio'], { type: 'audio/mp4; codecs=mp4a.40.2' });

describe('voice transcription', () => {
  it('reserves the complete upstream budget on the compatibility path', () => {
    const compatibilityRequestBudget = (
      VOICE_TRANSCRIPTION_TIMEOUT_MS
      - CLOUD_TOKEN_MINT_TIMEOUT_MS
    );

    expect(compatibilityRequestBudget).toBeGreaterThanOrEqual(150_000);
  });

  it('uses the real container extension and normalized MIME type', async () => {
    const telemetry = vi.fn();
    const cloudFetch = vi.fn().mockImplementation(async (_path: string, init?: RequestInit) => {
      const file = (init?.body as FormData).get('file') as File;
      expect(file.name).toBe('voice.mp4');
      expect(file.type).toBe('audio/mp4; codecs=mp4a.40.2');
      return Response.json({ text: 'hello' });
    });

    await expect(transcribeVoiceBlob(audioBlob(), {
      cloudFetch,
      durationMs: 58_000,
      attemptCount: 2,
      dictationId: 'dictation-123',
      telemetry,
    })).resolves.toBe('hello');
    expect(voiceRecordingFileName(audioBlob())).toBe('voice.mp4');
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      event: 'segment_transcription',
      outcome: 'success',
      path: 'cloud',
      providerStage: 'response',
      dictationId: 'dictation-123',
      sizeBytes: 5,
      mimeType: 'audio/mp4',
      durationMs: 58_000,
      attemptCount: 2,
    }));
  });

  it('uses the local compatibility route only when cloud credentials are unavailable', async () => {
    const cloudFetch = vi.fn().mockRejectedValue(new CloudUnavailableError());
    const localFetch = vi.fn().mockResolvedValue(Response.json({ text: 'local transcript' }));
    const telemetry = vi.fn();

    await expect(transcribeVoiceBlob(audioBlob(), {
      cloudFetch,
      localFetch,
      telemetry,
    })).resolves.toBe('local transcript');
    expect(localFetch).toHaveBeenCalledTimes(1);
    const payload = JSON.parse(String(localFetch.mock.calls[0]?.[1]?.body));
    expect(payload).toMatchObject({ name: 'voice.mp4', mime: 'audio/mp4' });
    expect(telemetry.mock.calls.map(([event]) => event)).toEqual([
      expect.objectContaining({
        path: 'cloud',
        providerStage: 'token',
        outcome: 'fallback',
      }),
      expect.objectContaining({
        path: 'local',
        providerStage: 'response',
        outcome: 'success',
      }),
    ]);
  });

  it('reports the hidden 401 and the refreshed cloud upload as separate attempts', async () => {
    const telemetry = vi.fn();
    const cloudFetch = vi.fn().mockImplementation(
      async (_path: string, init?: AvibeFetchRequestInit) => {
        init?.onAttempt?.({ phase: 'started', attempt: 1 });
        init?.onAttempt?.({
          phase: 'response',
          attempt: 1,
          status: 401,
          elapsedMs: 37,
        });
        init?.onAttempt?.({ phase: 'started', attempt: 2 });
        return Response.json({ text: 'after refresh' });
      },
    );

    await expect(transcribeVoiceBlob(audioBlob(), {
      cloudFetch,
      telemetry,
    })).resolves.toBe('after refresh');

    expect(telemetry.mock.calls.map(([event]) => event)).toEqual([
      expect.objectContaining({
        path: 'cloud',
        providerStage: 'response',
        outcome: 'failed',
        httpStatus: 401,
        elapsedMs: 37,
        attemptCount: 1,
        retry: false,
      }),
      expect.objectContaining({
        path: 'cloud',
        providerStage: 'response',
        outcome: 'success',
        attemptCount: 2,
        retry: true,
      }),
    ]);
  });

  it('does not double-report a terminal 401 after refresh', async () => {
    const telemetry = vi.fn();
    const cloudFetch = vi.fn().mockImplementation(
      async (_path: string, init?: AvibeFetchRequestInit) => {
        init?.onAttempt?.({ phase: 'started', attempt: 1 });
        init?.onAttempt?.({
          phase: 'response',
          attempt: 1,
          status: 401,
          elapsedMs: 12,
        });
        init?.onAttempt?.({ phase: 'started', attempt: 2 });
        init?.onAttempt?.({
          phase: 'response',
          attempt: 2,
          status: 401,
          elapsedMs: 9,
        });
        return Response.json({ error: 'unauthorized' }, { status: 401 });
      },
    );

    await expect(transcribeVoiceBlob(audioBlob(), {
      cloudFetch,
      telemetry,
    })).rejects.toMatchObject({ code: 'failed', status: 401 });

    expect(telemetry.mock.calls.map(([event]) => event)).toEqual([
      expect.objectContaining({
        outcome: 'failed',
        httpStatus: 401,
        attemptCount: 1,
      }),
      expect.objectContaining({
        outcome: 'failed',
        httpStatus: 401,
        attemptCount: 2,
      }),
    ]);
  });

  it('keeps the original deadline when the compatibility route starts late', async () => {
    vi.useFakeTimers();
    try {
      const cloudFetch = vi.fn(
        async () =>
          new Promise<Response>((_resolve, reject) => {
            setTimeout(() => reject(new CloudUnavailableError()), 80);
          }),
      );
      const localFetch = vi.fn(
        async (_path: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener(
              'abort',
              () => reject(init.signal?.reason),
              { once: true },
            );
          }),
      );

      const transcription = transcribeVoiceBlob(audioBlob(), {
        cloudFetch,
        localFetch,
        timeoutMs: 100,
      });
      const result = expect(transcription).rejects.toMatchObject({ code: 'timeout' });

      await vi.advanceTimersByTimeAsync(80);
      expect(localFetch).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(20);
      await result;
    } finally {
      vi.useRealTimers();
    }
  });

  it('preserves timeout classification from the compatibility route', async () => {
    const cloudFetch = vi.fn().mockRejectedValue(new CloudUnavailableError());
    const localFetch = vi.fn().mockResolvedValue(
      Response.json({ error: 'transcription_timeout' }, { status: 504 }),
    );
    const telemetry = vi.fn();

    await expect(transcribeVoiceBlob(audioBlob(), {
      cloudFetch,
      localFetch,
      telemetry,
    })).rejects.toMatchObject({ code: 'timeout', status: 504 });
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      path: 'local',
      providerStage: 'response',
      outcome: 'timeout',
      httpStatus: 504,
    }));
  });

  it('preserves empty-audio classification from the compatibility route', async () => {
    const cloudFetch = vi.fn().mockRejectedValue(new CloudUnavailableError());
    const localFetch = vi.fn().mockResolvedValue(
      Response.json({ error: 'transcription_empty' }, { status: 422 }),
    );
    const telemetry = vi.fn();

    await expect(transcribeVoiceBlob(audioBlob(), {
      cloudFetch,
      localFetch,
      telemetry,
    })).rejects.toMatchObject({ code: 'empty', status: 422 });
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      path: 'local',
      providerStage: 'response',
      outcome: 'empty',
      httpStatus: 422,
    }));
  });

  it('does not duplicate an audio upload after the cloud has returned an error', async () => {
    const cloudFetch = vi.fn().mockResolvedValue(
      Response.json({ error: 'transcription_failed' }, { status: 502 }),
    );
    const localFetch = vi.fn();

    await expect(transcribeVoiceBlob(audioBlob(), { cloudFetch, localFetch })).rejects.toMatchObject({
      code: 'failed',
      status: 502,
    });
    expect(localFetch).not.toHaveBeenCalled();
  });

  it('does not use the compatibility route after a cloud upload started', async () => {
    const telemetry = vi.fn();
    const cloudFetch = vi.fn().mockRejectedValue(
      new CloudUnavailableError('cloud_refresh_unavailable', { uploadStarted: true }),
    );
    const localFetch = vi.fn();

    await expect(
      transcribeVoiceBlob(audioBlob(), { cloudFetch, localFetch, telemetry }),
    ).rejects.toMatchObject({ code: 'unavailable' });
    expect(localFetch).not.toHaveBeenCalled();
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      outcome: 'unavailable',
      path: 'cloud',
      providerStage: 'refresh',
    }));
  });

  it('classifies direct cloud transport failures as unavailable without fallback', async () => {
    const telemetry = vi.fn();
    const cloudFetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    const localFetch = vi.fn();

    await expect(
      transcribeVoiceBlob(audioBlob(), { cloudFetch, localFetch, telemetry }),
    ).rejects.toMatchObject({ code: 'unavailable' });
    expect(localFetch).not.toHaveBeenCalled();
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      outcome: 'unavailable',
      path: 'cloud',
      providerStage: 'upload',
    }));
  });

  it.each([
    ['malformed JSON', () => new Response('{', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }), 'failed'],
    ['missing text', () => Response.json({ result: 'hello' }), 'failed'],
    ['empty text', () => Response.json({ text: '  ' }), 'empty'],
  ])('classifies a 2xx %s payload as %s', async (_case, response, expectedCode) => {
    const cloudFetch = vi.fn().mockImplementation(response);
    const telemetry = vi.fn();

    await expect(
      transcribeVoiceBlob(audioBlob(), { cloudFetch, telemetry }),
    ).rejects.toMatchObject({ code: expectedCode, status: 200 });
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      outcome: expectedCode,
      path: 'cloud',
      providerStage: 'response',
      httpStatus: 200,
    }));
  });

  it('stops a hung cloud request without starting the compatibility route', async () => {
    const cloudFetch = vi.fn().mockImplementation(
      async (_path: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
        }),
    );
    const localFetch = vi.fn();

    await expect(
      transcribeVoiceBlob(audioBlob(), { cloudFetch, localFetch, timeoutMs: 5 }),
    ).rejects.toMatchObject({ code: 'timeout' });
    expect(cloudFetch).toHaveBeenCalledTimes(1);
    expect(localFetch).not.toHaveBeenCalled();
  });

  it('reports deliberate cancellation separately from a deadline timeout', async () => {
    const controller = new AbortController();
    const telemetry = vi.fn();
    const cloudFetch = vi.fn().mockImplementation(
      async (_path: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
        }),
    );

    const transcription = transcribeVoiceBlob(audioBlob(), {
      cloudFetch,
      signal: controller.signal,
      telemetry,
    });
    controller.abort();

    await expect(transcription).rejects.toMatchObject({ code: 'cancelled' });
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      event: 'segment_transcription',
      outcome: 'cancelled',
      path: 'cloud',
    }));
    expect(telemetry).not.toHaveBeenCalledWith(expect.objectContaining({
      outcome: 'timeout',
    }));
  });

  it.each([
    [413, 'file_too_large', 'too_large'],
    [504, 'transcription_timeout', 'timeout'],
    [503, 'asr_not_configured', 'unavailable'],
    [400, 'asr_unavailable', 'unavailable'],
  ] as const)('maps status %s to %s', async (status, upstreamCode, expectedCode) => {
    const cloudFetch = vi.fn().mockResolvedValue(
      Response.json({ error: upstreamCode }, { status }),
    );

    await expect(transcribeVoiceBlob(audioBlob(), { cloudFetch })).rejects.toEqual(
      expect.objectContaining({ code: expectedCode, status }),
    );
  });

  it('joins independently transcribed segments in capture order', async () => {
    const segments = [
      { blob: new Blob(['one']), text: 'first' },
      { blob: new Blob(['two']) },
      { blob: new Blob(['three']) },
    ];
    const transcribe = vi.fn(async (blob: Blob) => blob.text());

    await transcribeVoiceSegments(segments, { concurrency: 2, transcribe });

    expect(transcribe).toHaveBeenCalledTimes(2);
    expect(voiceTranscriptFromSegments(segments)).toBe('first two three');
  });

  it('bounds incrementally queued initial transcriptions', async () => {
    const releases: Array<() => void> = [];
    let active = 0;
    let maxActive = 0;
    const transcribe = vi.fn(async (blob: Blob) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise<void>((resolve) => releases.push(resolve));
      active -= 1;
      return blob.text();
    });
    const queue = new VoiceTranscriptionQueue({ concurrency: 2, transcribe });
    const segments = Array.from(
      { length: 5 },
      (_value, index) => ({ blob: new Blob([String(index)]) }),
    );

    const tasks = segments.map((segment) => queue.enqueue(segment));
    expect(transcribe).toHaveBeenCalledTimes(2);

    releases.splice(0, 2).forEach((release) => release());
    await vi.waitFor(() => expect(transcribe).toHaveBeenCalledTimes(4));
    releases.splice(0, 2).forEach((release) => release());
    await vi.waitFor(() => expect(transcribe).toHaveBeenCalledTimes(5));
    releases.splice(0).forEach((release) => release());
    await Promise.all(tasks);

    expect(maxActive).toBe(2);
    expect(segments.map((segment) => segment.text)).toEqual(['0', '1', '2', '3', '4']);
    expect(segments.map((segment) => segment.attemptCount)).toEqual([1, 1, 1, 1, 1]);
  });

  it('cancels active retries without starting queued segments', async () => {
    const controller = new AbortController();
    const cloudFetch = vi.fn(
      (_path: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        }, { once: true });
      }),
    );
    const segments = Array.from(
      { length: 5 },
      () => ({ blob: audioBlob() }),
    );

    const transcription = transcribeVoiceSegments(segments, {
      cloudFetch,
      concurrency: 2,
      signal: controller.signal,
    });
    await vi.waitFor(() => expect(cloudFetch).toHaveBeenCalledTimes(2));
    controller.abort();
    await transcription;

    expect(cloudFetch).toHaveBeenCalledTimes(2);
    expect(segments.slice(0, 2)).toEqual([
      expect.objectContaining({
        attemptCount: 1,
        error: expect.objectContaining({ code: 'cancelled' }),
      }),
      expect.objectContaining({
        attemptCount: 1,
        error: expect.objectContaining({ code: 'cancelled' }),
      }),
    ]);
    expect(segments.slice(2)).toEqual([
      expect.not.objectContaining({ attemptCount: expect.anything() }),
      expect.not.objectContaining({ attemptCount: expect.anything() }),
      expect.not.objectContaining({ attemptCount: expect.anything() }),
    ]);
  });

  it('discards pending initial transcriptions when capture is discarded', async () => {
    const controller = new AbortController();
    const cloudFetch = vi.fn(
      (_path: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        }, { once: true });
      }),
    );
    const queue = new VoiceTranscriptionQueue({
      cloudFetch,
      concurrency: 2,
      signal: controller.signal,
    });
    const segments = Array.from(
      { length: 5 },
      () => ({ blob: audioBlob() }),
    );

    const tasks = segments.map((segment) => queue.enqueue(segment));
    await vi.waitFor(() => expect(cloudFetch).toHaveBeenCalledTimes(2));
    controller.abort();
    await Promise.all(tasks);

    expect(cloudFetch).toHaveBeenCalledTimes(2);
    expect(segments.slice(0, 2)).toEqual([
      expect.objectContaining({
        attemptCount: 1,
        error: expect.objectContaining({ code: 'cancelled' }),
      }),
      expect.objectContaining({
        attemptCount: 1,
        error: expect.objectContaining({ code: 'cancelled' }),
      }),
    ]);
    expect(segments.slice(2)).toEqual([
      expect.not.objectContaining({ attemptCount: expect.anything() }),
      expect.not.objectContaining({ attemptCount: expect.anything() }),
      expect.not.objectContaining({ attemptCount: expect.anything() }),
    ]);
  });

  it.each([
    [['你好', '世界'], '你好世界'],
    [['hello', 'world'], 'hello world'],
    [['hello ', 'world'], 'hello world'],
    [['123', '456'], '123456'],
    [['https://example.', 'com/path'], 'https://example.com/path'],
    [['https://example.', 'technology'], 'https://example.technology'],
    [['example.', 'cn/path'], 'example.cn/path'],
    [['example.', 'co.uk/path'], 'example.co.uk/path'],
    [['sentence.', 'cn'], 'sentence. cn'],
    [['voice_input-', 'reliability'], 'voice_input-reliability'],
    [['hello,', 'world'], 'hello, world'],
    [['use camelCase', 'notation matters'], 'use camelCase notation matters'],
    [['camel', 'Case'], 'camel Case'],
    [['we met', 'Alice yesterday'], 'we met Alice yesterday'],
    [['first paragraph\n\n', 'second paragraph'], 'first paragraph\n\nsecond paragraph'],
    [['hello.', 'world'], 'hello. world'],
  ])('joins segment boundaries without corrupting language or tokens', (parts, expected) => {
    expect(voiceTranscriptFromSegments(
      parts.map((text) => ({ blob: new Blob(), text })),
    )).toBe(expected);
  });

  it('skips silent segments unless the whole dictation is silent', () => {
    const empty = new VoiceTranscriptionError('empty');
    expect(voiceTranscriptFromSegments([
      { blob: new Blob(), text: 'first' },
      { blob: new Blob(), error: empty },
      { blob: new Blob(), text: 'second' },
    ])).toBe('first second');

    expect(() => voiceTranscriptFromSegments([
      { blob: new Blob(), error: empty },
      { blob: new Blob(), error: new VoiceTranscriptionError('empty') },
    ])).toThrowError(empty);
  });

  it('retries only failed segments without discarding completed text', async () => {
    const failed = new Error('provider failed');
    const segments = [
      { blob: new Blob(['one']) },
      { blob: new Blob(['two']) },
    ];
    const firstAttempt = vi
      .fn<(blob: Blob) => Promise<string>>()
      .mockResolvedValueOnce('first')
      .mockRejectedValueOnce(failed);

    await transcribeVoiceSegments(segments, { transcribe: firstAttempt });
    expect(() => voiceTranscriptFromSegments(segments)).toThrow(failed);

    const retry = vi.fn(async (blob: Blob) => blob.text());
    await transcribeVoiceSegments(segments, { transcribe: retry });

    expect(retry).toHaveBeenCalledTimes(1);
    expect(await retry.mock.calls[0]?.[0].text()).toBe('two');
    expect(voiceTranscriptFromSegments(segments)).toBe('first two');
  });
});
