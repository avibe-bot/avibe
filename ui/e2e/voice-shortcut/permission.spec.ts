import { expect, test, type Page } from '../support/browserTest';
import en from '../../src/i18n/en.json' with { type: 'json' };

// The real hold and AudioWorklet, on Chromium's generated 440 Hz input. Failures
// and stale website permissions are injected; this does not exercise macOS TCC.
async function composer(page: Page) {
  await page.goto('/e2e/voice-shortcut/fixture.html');
  const box = page.locator('[data-hold-to-talk="ready"]');
  await expect(box).toBeVisible();
  const rect = (await box.boundingBox())!;
  await page.mouse.move(rect.x + rect.width * 0.4, rect.y + 22);
}

test('an explicit Desktop retry reaches capture after refusal and delivers frames on repeated holds', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
    const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    let requests = 0;
    navigator.mediaDevices.getUserMedia = (constraints) => ++requests === 1
      ? Promise.reject(new DOMException('Injected refusal', 'NotAllowedError'))
      : getUserMedia(constraints);
    // Stale website state remains denied even after the simulated OS change.
    const query = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (descriptor) => descriptor.name === 'microphone'
      ? Promise.resolve({ state: 'denied' } as PermissionStatus)
      : query(descriptor);
  });
  await composer(page);
  await page.mouse.down();
  await expect(page.getByText(en.voice.deniedDesktop)).toBeVisible();
  await page.mouse.up();
  expect(await page.evaluate(() => window.voiceShortcutTest.spoken)).toEqual([]);

  for (let index = 0; index < 2; index += 1) {
    await page.mouse.down();
    await expect(page.getByText(en.voice.composer.releaseToSend, { exact: true })).toBeVisible();
    await page.waitForTimeout(1_300);
    await page.mouse.up();
    await expect.poll(() => page.evaluate(() => window.voiceShortcutTest.spoken.length)).toBe(index + 1);
  }
  const traces = await page.evaluate(() => window.voiceCaptureEvents.filter((event) => event.event === 'capture_trace'));
  expect(traces[0]).toMatchObject({
    surface: 'app_shell', origin: 'loopback', mic: 'NotAllowedError', captureClass: 'no_microphone',
  });
  const captured = traces.filter((event) => event.mic === 'ok');
  expect(captured).toHaveLength(2);
  for (const trace of captured) {
    expect(trace.captureClass).toBe('sound');
    expect(Number(trace.chunks)).toBeGreaterThan(0);
    expect(Number(trace.peakDbfs)).toBeGreaterThan(-25);
    expect((trace.attempts as { renderCalls?: number }[]).some((attempt) => Number(attempt.renderCalls) > 0)).toBe(true);
  }
  const stats = captured.map(({ chunks, levels, peakDbfs, attempts }) => ({ chunks, levels, peakDbfs, attempts }));
  console.log(`Synthetic input statistics: ${JSON.stringify(stats)}`);
  await test.info().attach('synthetic-input-statistics', { body: JSON.stringify(stats, null, 2), contentType: 'application/json' });
});

for (const [error, text] of [
  ['NotAllowedError', en.voice.deniedTitle],
  ['NotFoundError', en.voice.noDevice],
  ['NotReadableError', en.voice.composer.micBusy],
  ['AbortError', en.voice.micFailed],
] as const) {
  test(`a first permission probe reports ${error} and logs only metadata`, async ({ page }) => {
    await page.addInitScript((name) => {
      const query = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (descriptor) => descriptor.name === 'microphone'
        ? Promise.resolve({ state: 'prompt' } as PermissionStatus)
        : query(descriptor);
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Injected failure', name));
      navigator.mediaDevices.enumerateDevices = async () => [];
    }, error);
    await composer(page);
    for (let index = 0; index < 2; index += 1) {
      await page.mouse.down();
      await expect(page.getByText(text, { exact: true })).toBeVisible();
      await page.mouse.up();
    }
    await expect.poll(() => page.evaluate(() => window.voiceCaptureEvents.filter((event) => event.event === 'capture_trace').length)).toBe(2);
    const traces = await page.evaluate(() => window.voiceCaptureEvents.filter((event) => event.event === 'capture_trace'));
    for (const trace of traces) {
      expect(trace).toMatchObject({ mic: error, origin: 'loopback', audioInputCount: 0, captureClass: 'no_microphone', chunks: 0 });
    }
    expect(await page.evaluate(() => window.voiceShortcutTest.spoken)).toEqual([]);
  });
}
