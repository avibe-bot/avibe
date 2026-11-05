import { describe, expect, it, vi } from 'vitest';

import {
  CloudUnavailableError,
  CLOUD_TOKEN_MINT_TIMEOUT_MS,
  type AvibeFetchRequestInit,
} from './avibeFetch';
import {
  finalizeVoiceDictation,
  transcribeVoiceBlob,
  VoiceTranscriptionQueue,
  type VoiceTranscriptionSegment,
  transcribeVoiceSegments,
  VOICE_TRANSCRIPTION_TIMEOUT_MS,
  voiceDictationAttemptTimeoutMs,
  voiceRecordingFileName,
  VoiceTranscriptionError,
} from './voiceTranscription';

const audioBlob = () => new Blob(['audio'], { type: 'audio/mp4; codecs=mp4a.40.2' });
/** The form an attempt sends, built for a token with these capabilities. */
const formOf = (init: AvibeFetchRequestInit | undefined, capabilities: readonly string[] = []): FormData => {
  const body = init?.bodyFor?.(capabilities) ?? init?.body;
  expect(body).toBeInstanceOf(FormData);
  if (!(body instanceof FormData)) throw new Error('Expected a multipart transcription request');
  return body;
};

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
    const cloudFetch = vi.fn().mockImplementation(async (path: string, init?: AvibeFetchRequestInit) => {
      expect(path).toBe('/api/cloud/audio/transcriptions');
      const file = formOf(init).get('file') as File;
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

  it('retains accepted receipts and uploads only missing intermediate segments', async () => {
    const segments: VoiceTranscriptionSegment[] = [
      { blob: new Blob(['one']), sequence: 0, final: false, receipt: 'accepted' },
      { blob: new Blob(['two']), sequence: 1, final: false },
      { blob: new Blob(['three']), sequence: 2, final: false },
      { blob: new Blob(['final']), sequence: 3, final: true },
    ];
    const transcribe = vi.fn(async (blob: Blob) => blob.text());

    await transcribeVoiceSegments(segments, { concurrency: 2, transcribe });

    expect(transcribe).toHaveBeenCalledTimes(2);
    expect(segments.map((segment) => segment.receipt)).toEqual([
      'accepted',
      'two',
      'three',
      undefined,
    ]);
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
    const segments: VoiceTranscriptionSegment[] = Array.from(
      { length: 5 },
      (_value, index) => ({
        blob: new Blob([String(index)]),
        sequence: index,
        final: false,
      }),
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
    expect(segments.map((segment) => segment.receipt)).toEqual(['0', '1', '2', '3', '4']);
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
    const segments: VoiceTranscriptionSegment[] = Array.from(
      { length: 5 },
      (_value, index) => ({ blob: audioBlob(), sequence: index, final: false }),
    );

    const transcription = transcribeVoiceSegments(segments, {
      cloudFetch,
      concurrency: 2,
      dictationId: 'dictation-cancelled',
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
      dictationId: 'dictation-discarded',
      signal: controller.signal,
    });
    const segments: VoiceTranscriptionSegment[] = Array.from(
      { length: 5 },
      (_value, index) => ({ blob: audioBlob(), sequence: index, final: false }),
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

  it('submits ordered receipts and cursor context with the final audio segment', async () => {
    const cloudFetch = vi.fn(async (path: string, init?: AvibeFetchRequestInit) => {
      expect(path).toBe('/api/cloud/voice/dictations');
      const form = formOf(init);
      if (form.get('final') === 'false') {
        expect(form.get('sequence')).toBe('0');
        expect(form.getAll('receipt')).toEqual([]);
        return Response.json({ receipt: 'receipt-0', sequence: 0 });
      }
      expect(form.get('sequence')).toBe('1');
      expect(form.get('finalize_only')).toBeNull();
      expect(form.getAll('receipt')).toEqual(['receipt-0']);
      expect(form.get('before')).toBe('prefix ');
      expect(form.get('after')).toBe(' suffix');
      expect(form.get('file')).toBeInstanceOf(Blob);
      return Response.json({ text: 'cleaned result', cleanup: 'success' });
    });
    const segments: VoiceTranscriptionSegment[] = [
      { blob: audioBlob(), sequence: 0, final: false },
      { blob: audioBlob(), sequence: 1, final: true, overlapMs: 250 },
    ];

    await transcribeVoiceSegments(segments, {
      cloudFetch,
      dictationId: 'dictation-final',
    });
    await expect(finalizeVoiceDictation(segments, {
      dictationId: 'dictation-final',
      before: 'prefix ',
      after: ' suffix',
    }, { cloudFetch })).resolves.toEqual({ text: 'cleaned result', cleanup: 'success' });

    expect(cloudFetch).toHaveBeenCalledTimes(2);
    expect(segments[0]?.receipt).toBe('receipt-0');
  });

  it('sends the conversation with every segment only to a backend that declares dictation context', async () => {
    const forms: Array<{ declared: FormData; undeclared: FormData }> = [];
    const cloudFetch = vi.fn(async (_path: string, init?: AvibeFetchRequestInit) => {
      const declared = formOf(init, ['dictation_context']);
      forms.push({ declared, undeclared: formOf(init, []) });
      return declared.get('final') === 'false'
        ? Response.json({ receipt: 'receipt-0', sequence: 0 })
        : Response.json({ text: '结果', cleanup: 'success' });
    });
    const segments: VoiceTranscriptionSegment[] = [
      { blob: audioBlob(), sequence: 0, final: false },
      { blob: audioBlob(), sequence: 1, final: true },
    ];
    const context = Promise.resolve({ reply: '先跑 make release', userPrevious: '部署前要做什么' });

    await transcribeVoiceSegments(segments, { cloudFetch, dictationId: 'dictation-context', context });
    await finalizeVoiceDictation(segments, { dictationId: 'dictation-context', before: '', after: '' }, { cloudFetch, context });

    expect(forms).toHaveLength(2);
    for (const { declared, undeclared } of forms) {
      expect(declared.get('reply')).toBe('先跑 make release');
      expect(declared.get('user_previous')).toBe('部署前要做什么');
      // An older avibe.bot gets exactly the form it always did.
      expect([...undeclared.keys()]).not.toContain('reply');
      expect([...undeclared.keys()]).not.toContain('user_previous');
    }
  });

  it('sends the names to hear exactly with every segment only to a backend that declares dictation vocabulary', async () => {
    const forms: Array<{ declared: FormData; contextOnly: FormData }> = [];
    const cloudFetch = vi.fn(async (_path: string, init?: AvibeFetchRequestInit) => {
      const declared = formOf(init, ['dictation_context', 'dictation_vocabulary']);
      forms.push({ declared, contextOnly: formOf(init, ['dictation_context']) });
      return declared.get('final') === 'false'
        ? Response.json({ receipt: 'receipt-0', sequence: 0 })
        : Response.json({ text: '结果', cleanup: 'success' });
    });
    const segments: VoiceTranscriptionSegment[] = [
      { blob: audioBlob(), sequence: 0, final: false },
      { blob: audioBlob(), sequence: 1, final: true },
    ];
    const context = Promise.resolve({ reply: '先跑 make release', vocabulary: ['歪比', 'Product'] });

    await transcribeVoiceSegments(segments, { cloudFetch, dictationId: 'dictation-names', context });
    await finalizeVoiceDictation(segments, { dictationId: 'dictation-names', before: '', after: '' }, { cloudFetch, context });

    expect(forms).toHaveLength(2);
    for (const { declared, contextOnly } of forms) {
      expect(declared.getAll('vocabulary')).toEqual(['歪比', 'Product']);
      expect(declared.get('reply')).toBe('先跑 make release');
      // An avibe.bot that does not declare it gets the form it reads today.
      expect(contextOnly.getAll('vocabulary')).toEqual([]);
      expect(contextOnly.get('reply')).toBe('先跑 make release');
    }
  });

  it('cuts the conversation to its newest 8,000 characters on the HTTP path', async () => {
    let form: FormData | undefined;
    const cloudFetch = vi.fn(async (_path: string, init?: AvibeFetchRequestInit) => {
      form = formOf(init, ['dictation_context']);
      return Response.json({ receipt: 'receipt-0', sequence: 0 });
    });
    const segments: VoiceTranscriptionSegment[] = [{ blob: audioBlob(), sequence: 0, final: false }];

    await transcribeVoiceSegments(segments, {
      cloudFetch,
      dictationId: 'dictation-long',
      context: { reply: `开头${'x'.repeat(8_000)}`, userPrevious: '  ' },
    });

    expect(form?.get('reply')).toBe('x'.repeat(8_000));
    expect(form?.get('user_previous')).toBeNull();
  });

  it('uses a finalize-only request when capture stops exactly at a segment boundary', async () => {
    const cloudFetch = vi.fn(async (_path: string, init?: AvibeFetchRequestInit) => {
      const form = formOf(init);
      expect(form.get('file')).toBeNull();
      expect(form.get('finalize_only')).toBe('true');
      expect(form.getAll('receipt')).toEqual(['receipt-0']);
      return Response.json({ text: 'boundary result', cleanup: 'fallback' });
    });
    const segments: VoiceTranscriptionSegment[] = [
      { blob: audioBlob(), sequence: 0, final: false, receipt: 'receipt-0' },
      { blob: null, sequence: 1, final: true },
    ];

    await expect(finalizeVoiceDictation(segments, {
      dictationId: 'dictation-boundary',
      before: '',
      after: '',
    }, { cloudFetch })).resolves.toEqual({ text: 'boundary result', cleanup: 'fallback' });
  });

  it('reuploads retained segments after the server rejects their receipts', async () => {
    const cloudFetch = vi.fn().mockResolvedValue(
      Response.json({ error: 'invalid_dictation' }, { status: 422 }),
    );
    const segments: VoiceTranscriptionSegment[] = [
      { blob: audioBlob(), sequence: 0, final: false, receipt: 'stale-receipt' },
      { blob: null, sequence: 1, final: true },
    ];

    await expect(finalizeVoiceDictation(segments, {
      dictationId: 'dictation-stale',
      before: '',
      after: '',
    }, { cloudFetch })).rejects.toMatchObject({ code: 'failed', status: 422 });
    expect(segments[0]?.receipt).toBeUndefined();

    const transcribe = vi.fn().mockResolvedValue('replacement-receipt');
    await transcribeVoiceSegments(segments, { transcribe });
    expect(transcribe).toHaveBeenCalledOnce();
    expect(segments[0]?.receipt).toBe('replacement-receipt');
  });

  it('forwards the same finalization contract through the local compatibility route', async () => {
    const cloudFetch = vi.fn().mockRejectedValue(new CloudUnavailableError());
    const localFetch = vi.fn(async (_path: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      if (body.final === false) return Response.json({ receipt: 'local-receipt', sequence: 0 });
      expect(body).toMatchObject({
        dictation_id: 'dictation-local',
        sequence: 1,
        final: true,
        finalize_only: true,
        receipts: ['local-receipt'],
        before: '前文',
        after: '后文',
        reply: '上一条回复',
        user_previous: '上一条消息',
        vocabulary: ['歪比'],
      });
      expect(body.data).toBeUndefined();
      return Response.json({ text: '本地代理结果', cleanup: 'success' });
    });
    const segments: VoiceTranscriptionSegment[] = [
      { blob: audioBlob(), sequence: 0, final: false },
      { blob: null, sequence: 1, final: true },
    ];

    await transcribeVoiceSegments(segments, {
      cloudFetch,
      localFetch,
      dictationId: 'dictation-local',
    });
    await expect(finalizeVoiceDictation(segments, {
      dictationId: 'dictation-local',
      before: '前文',
      after: '后文',
    }, { cloudFetch, localFetch, context: { reply: '上一条回复', userPrevious: '上一条消息', vocabulary: ['歪比'] } })).resolves.toEqual({
      text: '本地代理结果',
      cleanup: 'success',
    });
  });

  it('retries only failed intermediate segments without discarding receipts', async () => {
    const failed = new Error('provider failed');
    const segments: VoiceTranscriptionSegment[] = [
      { blob: new Blob(['one']), sequence: 0, final: false },
      { blob: new Blob(['two']), sequence: 1, final: false },
      { blob: new Blob(['final']), sequence: 2, final: true },
    ];
    const firstAttempt = vi
      .fn<(blob: Blob) => Promise<string>>()
      .mockResolvedValueOnce('first')
      .mockRejectedValueOnce(failed);

    await transcribeVoiceSegments(segments, { transcribe: firstAttempt });
    expect(segments[0]).toMatchObject({ receipt: 'first', error: undefined });
    expect(segments[1]?.error).toBe(failed);

    const retry = vi.fn(async (blob: Blob) => blob.text());
    await transcribeVoiceSegments(segments, { transcribe: retry });

    expect(retry).toHaveBeenCalledTimes(1);
    expect(await retry.mock.calls[0]?.[0].text()).toBe('two');
    expect(segments.slice(0, 2).map((segment) => segment.receipt)).toEqual(['first', 'two']);

    const finalize = vi.fn().mockResolvedValue({ text: 'final', cleanup: 'success' as const });
    await finalizeVoiceDictation(segments, {
      dictationId: 'dictation-retry',
      before: '',
      after: '',
    }, { finalize });
    expect(finalize).toHaveBeenCalledWith({
      blob: segments[2]?.blob,
      sequence: 2,
      receipts: ['first', 'two'],
    });
  });

  describe('a take is never dropped for one failed request', () => {
    it('recognizes the retained audio again after the take came back empty, even at a segment boundary', async () => {
      let final = 0;
      const cloudFetch = vi.fn(async (_path: string, init?: AvibeFetchRequestInit) => {
        const form = formOf(init);
        if (form.get('final') === 'false') return Response.json({ receipt: `fresh-${form.get('sequence')}`, sequence: Number(form.get('sequence')) });
        final += 1;
        return final === 1
          ? Response.json({ text: '', cleanup: 'fallback' })
          : Response.json({ text: 'heard this time', cleanup: 'success' });
      });
      const segments: VoiceTranscriptionSegment[] = [
        { blob: audioBlob(), sequence: 0, final: false, receipt: 'empty-receipt' },
        { blob: null, sequence: 1, final: true },
      ];
      const input = { dictationId: 'dictation-empty', before: '', after: '' };

      await expect(finalizeVoiceDictation(segments, input, { cloudFetch, retryDelaysMs: [] })).rejects.toMatchObject({ code: 'empty' });
      expect(segments[0]?.receipt).toBeUndefined();
      await expect(finalizeVoiceDictation(segments, input, { cloudFetch, retryDelaysMs: [] })).resolves.toEqual({ text: 'heard this time', cleanup: 'success' });
      expect(segments[0]?.receipt).toBe('fresh-0');
    });

    it('starts no new attempt for a failed segment after the window of the take, but still sends one never tried', async () => {
      const cloudFetch = vi.fn(async () => Response.json({ receipt: 'receipt-2', sequence: 2 }));
      const segments: VoiceTranscriptionSegment[] = [
        { blob: audioBlob(), sequence: 0, final: false, receipt: 'receipt-0' },
        { blob: audioBlob(), sequence: 1, final: false, error: new VoiceTranscriptionError('unavailable') },
        { blob: audioBlob(), sequence: 2, final: false },
        { blob: null, sequence: 3, final: true },
      ];

      await expect(finalizeVoiceDictation(segments, {
        dictationId: 'dictation-window',
        before: '',
        after: '',
      }, { cloudFetch, retryUntil: Date.now() - 1 })).rejects.toMatchObject({ code: 'unavailable' });

      expect(cloudFetch).toHaveBeenCalledOnce();
      expect(formOf(cloudFetch.mock.calls[0]?.[1] as AvibeFetchRequestInit | undefined).get('sequence')).toBe('2');
      expect(segments[2]?.receipt).toBe('receipt-2');
    });

    const minuteOfAudio = () => new Blob([new Uint8Array(1_920_044)], { type: 'audio/wav' });

    it('sends a segment through the local relay when the cloud request never got an answer', async () => {
      const cloudFetch = vi.fn().mockRejectedValue(new TypeError('Load failed'));
      const localFetch = vi.fn().mockResolvedValue(Response.json({ receipt: 'relay-receipt', sequence: 0 }));
      const segments: VoiceTranscriptionSegment[] = [{ blob: audioBlob(), sequence: 0, final: false }];

      await transcribeVoiceSegments(segments, { cloudFetch, localFetch, dictationId: 'dictation-relay', retryDelaysMs: [] });

      expect(localFetch).toHaveBeenCalledWith('/api/asr/transcribe', expect.anything());
      expect(segments[0]).toMatchObject({ receipt: 'relay-receipt', error: undefined });
    });

    it('tries a segment again after the network or the service fails, and keeps the receipt', async () => {
      const cloudFetch = vi.fn()
        .mockResolvedValueOnce(Response.json({ error: 'asr_unavailable' }, { status: 503 }))
        .mockResolvedValueOnce(Response.json({ error: 'transcription_failed' }, { status: 502 }))
        .mockResolvedValueOnce(Response.json({ receipt: 'third-time', sequence: 0 }));
      const segments: VoiceTranscriptionSegment[] = [{ blob: audioBlob(), sequence: 0, final: false }];

      await transcribeVoiceSegments(segments, { cloudFetch, dictationId: 'dictation-flaky', retryDelaysMs: [0, 0] });

      expect(cloudFetch).toHaveBeenCalledTimes(3);
      expect(segments[0]).toMatchObject({ receipt: 'third-time', error: undefined, attemptCount: 3 });
    });

    it('does not repeat a request that cannot change its answer', async () => {
      const cloudFetch = vi.fn().mockResolvedValue(Response.json({ error: 'file_too_large' }, { status: 413 }));
      const segments: VoiceTranscriptionSegment[] = [{ blob: audioBlob(), sequence: 0, final: false }];

      await transcribeVoiceSegments(segments, { cloudFetch, dictationId: 'dictation-large', retryDelaysMs: [0, 0] });

      expect(cloudFetch).toHaveBeenCalledOnce();
      expect(segments[0]?.error).toMatchObject({ code: 'too_large' });
    });

    it('fails at once, without a request, while the browser has no network', async () => {
      const cloudFetch = vi.fn();
      const localFetch = vi.fn();
      const segments: VoiceTranscriptionSegment[] = [{ blob: audioBlob(), sequence: 0, final: false }];

      await transcribeVoiceSegments(segments, { cloudFetch, localFetch, dictationId: 'dictation-offline', online: () => false });

      expect(cloudFetch).not.toHaveBeenCalled();
      expect(localFetch).not.toHaveBeenCalled();
      expect(segments[0]?.error).toMatchObject({ code: 'unavailable' });
    });

    it('gives a hung dictation request its own deadline, sized to what it uploads', async () => {
      vi.useFakeTimers();
      try {
        const cloudFetch = vi.fn((_path: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
        }));
        const blob = minuteOfAudio();
        const segments: VoiceTranscriptionSegment[] = [{ blob, sequence: 0, final: false }];
        const deadline = voiceDictationAttemptTimeoutMs(blob.size, false);
        expect(deadline).toBeLessThan(VOICE_TRANSCRIPTION_TIMEOUT_MS / 3);

        const transcription = transcribeVoiceSegments(segments, { cloudFetch, dictationId: 'dictation-hung', retryDelaysMs: [] });
        await vi.advanceTimersByTimeAsync(deadline);
        await transcription;

        expect(segments[0]?.error).toMatchObject({ code: 'timeout' });
      } finally {
        vi.useRealTimers();
      }
    });

    it('sends a failed segment again before the last one, so one failed minute does not void the take', async () => {
      const cloudFetch = vi.fn(async (_path: string, init?: AvibeFetchRequestInit) => {
        const form = formOf(init);
        if (form.get('final') === 'false') return Response.json({ receipt: 'receipt-1', sequence: 1 });
        expect(form.getAll('receipt')).toEqual(['receipt-0', 'receipt-1']);
        return Response.json({ text: 'all of it', cleanup: 'success' });
      });
      const segments: VoiceTranscriptionSegment[] = [
        { blob: audioBlob(), sequence: 0, final: false, receipt: 'receipt-0' },
        { blob: audioBlob(), sequence: 1, final: false, error: new VoiceTranscriptionError('unavailable') },
        { blob: audioBlob(), sequence: 2, final: true },
      ];

      await expect(finalizeVoiceDictation(segments, {
        dictationId: 'dictation-resend',
        before: '',
        after: '',
      }, { cloudFetch, retryDelaysMs: [] })).resolves.toEqual({ text: 'all of it', cleanup: 'success' });
      expect(cloudFetch).toHaveBeenCalledTimes(2);
    });

    it('tries the last segment again after a transient failure', async () => {
      const cloudFetch = vi.fn()
        .mockRejectedValueOnce(new TypeError('Load failed'))
        .mockResolvedValueOnce(Response.json({ text: 'landed', cleanup: 'success' }));
      const localFetch = vi.fn().mockRejectedValue(new TypeError('Load failed'));
      const segments: VoiceTranscriptionSegment[] = [{ blob: audioBlob(), sequence: 0, final: true }];

      await expect(finalizeVoiceDictation(segments, {
        dictationId: 'dictation-final-retry',
        before: '',
        after: '',
      }, { cloudFetch, localFetch, retryDelaysMs: [0] })).resolves.toEqual({ text: 'landed', cleanup: 'success' });
      expect(cloudFetch).toHaveBeenCalledTimes(2);
      expect(localFetch).toHaveBeenCalledOnce();
    });
  });
});
