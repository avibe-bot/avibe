import { describe, expect, it, vi } from 'vitest';

import { CloudUnavailableError } from './avibeFetch';
import {
  transcribeVoiceBlob,
  voiceRecordingFileName,
} from './voiceTranscription';

const audioBlob = () => new Blob(['audio'], { type: 'audio/mp4; codecs=mp4a.40.2' });

describe('voice transcription', () => {
  it('uses the real container extension and normalized MIME type', async () => {
    const cloudFetch = vi.fn().mockImplementation(async (_path: string, init?: RequestInit) => {
      const file = (init?.body as FormData).get('file') as File;
      expect(file.name).toBe('voice.mp4');
      expect(file.type).toBe('audio/mp4; codecs=mp4a.40.2');
      return Response.json({ text: 'hello' });
    });

    await expect(transcribeVoiceBlob(audioBlob(), { cloudFetch })).resolves.toBe('hello');
    expect(voiceRecordingFileName(audioBlob())).toBe('voice.mp4');
  });

  it('uses the local compatibility route only when cloud credentials are unavailable', async () => {
    const cloudFetch = vi.fn().mockRejectedValue(new CloudUnavailableError());
    const localFetch = vi.fn().mockResolvedValue(Response.json({ text: 'local transcript' }));

    await expect(transcribeVoiceBlob(audioBlob(), { cloudFetch, localFetch })).resolves.toBe(
      'local transcript',
    );
    expect(localFetch).toHaveBeenCalledTimes(1);
    const payload = JSON.parse(String(localFetch.mock.calls[0]?.[1]?.body));
    expect(payload).toMatchObject({ name: 'voice.mp4', mime: 'audio/mp4' });
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

  it.each([
    [413, 'file_too_large', 'too_large'],
    [504, 'transcription_timeout', 'timeout'],
    [503, 'asr_not_configured', 'unavailable'],
  ] as const)('maps status %s to %s', async (status, upstreamCode, expectedCode) => {
    const cloudFetch = vi.fn().mockResolvedValue(
      Response.json({ error: upstreamCode }, { status }),
    );

    await expect(transcribeVoiceBlob(audioBlob(), { cloudFetch })).rejects.toEqual(
      expect.objectContaining({ code: expectedCode, status }),
    );
  });
});
