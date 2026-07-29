import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  browserFamilyFromUserAgent,
  emitVoiceTelemetry,
} from './voiceTelemetry';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('voice telemetry', () => {
  it.each([
    ['CriOS/126.0 Mobile/15E148 Safari/604.1', 'chrome'],
    ['FxiOS/127.0 Mobile/15E148 Safari/605.1.15', 'firefox'],
    ['EdgiOS/126.0 Mobile/15E148 Safari/605.1.15', 'edge'],
  ])('classifies iOS browser token %s as %s', (userAgent, expected) => {
    expect(browserFamilyFromUserAgent(userAgent)).toBe(expected);
  });

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
