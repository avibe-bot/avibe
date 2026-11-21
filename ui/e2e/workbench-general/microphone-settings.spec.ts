import { expect, test } from '../support/browserTest';
import { serveProduct } from './support';
import en from '../../src/i18n/en.json' with { type: 'json' };
import zh from '../../src/i18n/zh.json' with { type: 'json' };

// Real settings route + capture helper + level graph; only the input device is synthetic.
// Unit selection cases cannot detect missing route assembly or a live capture left by navigation.
for (const lang of ['en', 'zh'] as const) {
  test(`microphone settings ${lang}: explicit selected-device test stops on exit`, async ({ page }) => {
    const copy = (lang === 'en' ? en : zh).settings.microphone;
    await page.setViewportSize({ width: 880, height: 600 });
    await serveProduct(page, lang);
    await page.addInitScript(() => {
      const captures: { constraints: MediaStreamConstraints; track: MediaStreamTrack }[] = [];
      Object.defineProperty(window, 'micSettingsCaptures', { value: captures });
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
        enumerateDevices: async () => [{ kind: 'audioinput', deviceId: 'fixture-usb', label: 'Fixture USB' }],
        getUserMedia: async (constraints: MediaStreamConstraints) => {
          const context = new AudioContext();
          const oscillator = context.createOscillator();
          const destination = context.createMediaStreamDestination();
          oscillator.connect(destination);
          oscillator.start();
          await context.resume();
          const track = destination.stream.getAudioTracks()[0];
          const stop = track.stop.bind(track);
          track.stop = () => { stop(); oscillator.stop(); void context.close(); };
          captures.push({ constraints, track });
          return destination.stream;
        },
      } });
    });
    const captures = () => page.evaluate(() => (window as unknown as {
      micSettingsCaptures: { constraints: MediaStreamConstraints; track: MediaStreamTrack }[];
    }).micSettingsCaptures.map(({ constraints, track }) => ({ constraints, state: track.readyState })));
    await page.goto('/settings/microphone');
    await expect(page.getByRole('button', { name: copy.startTest, exact: true })).toBeVisible();
    expect(await captures()).toEqual([]);
    await page.getByRole('button', { name: copy.chooseInput }).click();
    await page.getByRole('button', { name: /Fixture USB/ }).click();
    await expect(page.getByRole('group', { name: copy.deviceList })).toBeHidden();
    await page.getByRole('button', { name: copy.startTest, exact: true }).click();
    await expect(page.getByRole('button', { name: copy.stopTest, exact: true })).toBeVisible();
    await expect(page.getByTestId('microphone-level')).toBeVisible();
    expect(await captures()).toEqual([{ constraints: { audio: { deviceId: { exact: 'fixture-usb' } } }, state: 'live' }]);
    await page.screenshot({ animations: 'disabled', path: `/tmp/w1-mic-composition-evidence/microphone-${lang}-testing.png` });
    await page.getByRole('button', { name: copy.stopTest, exact: true }).click();
    await expect.poll(async () => (await captures())[0].state).toBe('ended');
    await page.getByRole('button', { name: copy.startTest, exact: true }).click();
    await expect(page.getByRole('button', { name: copy.stopTest, exact: true })).toBeVisible();
    // A client-side route exit runs the actual settings owner's cleanup in the same document.
    await page.getByRole('link', { name: (lang === 'en' ? en : zh).settings.sections.shortcuts, exact: true }).click();
    await expect.poll(async () => (await captures()).map(item => item.state)).toEqual(['ended', 'ended']);
  });
}
