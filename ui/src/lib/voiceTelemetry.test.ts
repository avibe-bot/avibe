import { afterEach, describe, expect, it, vi } from 'vitest';

import { emitVoiceTelemetry } from './voiceTelemetry';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('voice telemetry', () => {
  it('emits privacy-safe structured metadata without voice contents', async () => {
    vi.stubGlobal('window', {});
    vi.stubGlobal('navigator', { userAgent: 'Mozilla/5.0 Chrome/126.0 Safari/537.36' });
    const telemetryFetch = vi.fn().mockResolvedValue(Response.json({ ok: true }));

    emitVoiceTelemetry({
      event: 'segment_transcription',
      outcome: 'success',
      path: 'cloud',
      providerStage: 'response',
      sizeBytes: 240_000,
      mimeType: 'audio/webm',
      durationMs: 60_000,
      elapsedMs: 820,
      attemptCount: 1,
    }, telemetryFetch);

    await vi.waitFor(() => expect(telemetryFetch).toHaveBeenCalledOnce());
    const [path, init] = telemetryFetch.mock.calls[0]!;
    const payload = JSON.parse(String(init?.body));
    expect(path).toBe('/api/asr/telemetry');
    expect(init).toMatchObject({ method: 'POST', keepalive: true });
    expect(payload).toEqual({
      event: 'segment_transcription',
      outcome: 'success',
      path: 'cloud',
      providerStage: 'response',
      sizeBytes: 240_000,
      mimeType: 'audio/webm',
      durationMs: 60_000,
      elapsedMs: 820,
      attemptCount: 1,
      browserFamily: 'chrome',
    });
    expect(payload).not.toHaveProperty('audio');
    expect(payload).not.toHaveProperty('transcript');
    expect(payload).not.toHaveProperty('credential');
  });
});
