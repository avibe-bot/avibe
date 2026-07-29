import { describe, expect, it, vi } from 'vitest';

import { CloudUnavailableError } from './avibeFetch';
import {
  transcribeVoiceBlob,
  transcribeVoiceSegments,
  voiceTranscriptFromSegments,
  voiceRecordingFileName,
} from './voiceTranscription';

const audioBlob = () => new Blob(['audio'], { type: 'audio/mp4; codecs=mp4a.40.2' });

describe('voice transcription', () => {
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
      telemetry,
    })).resolves.toBe('hello');
    expect(voiceRecordingFileName(audioBlob())).toBe('voice.mp4');
    expect(telemetry).toHaveBeenCalledWith(expect.objectContaining({
      event: 'segment_transcription',
      outcome: 'success',
      path: 'cloud',
      providerStage: 'response',
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
    const cloudFetch = vi.fn().mockRejectedValue(
      new CloudUnavailableError('cloud_refresh_unavailable', { uploadStarted: true }),
    );
    const localFetch = vi.fn();

    await expect(
      transcribeVoiceBlob(audioBlob(), { cloudFetch, localFetch }),
    ).rejects.toMatchObject({ code: 'failed' });
    expect(localFetch).not.toHaveBeenCalled();
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
