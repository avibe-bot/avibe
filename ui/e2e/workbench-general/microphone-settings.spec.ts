import { expect, test } from '../support/browserTest';
import { open, serveProduct } from './support';
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

// Layout is a browser contract: jsdom cannot detect a stretched trigger or a press transform.
// A selected long label must truncate within the available width while the arrow stays visible.
for (const lang of ['en', 'zh'] as const) {
  for (const theme of ['light', 'dark'] as const) {
    test(`microphone selector ${lang} ${theme}: content width, stable press and bounded long names`, async ({ page }) => {
      const copy = (lang === 'en' ? en : zh).settings.microphone;
      const longName = 'Studio USB microphone — Conference room input / 会议室外接麦克风 (High Definition Audio Device)';
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.emulateMedia({ reducedMotion: 'no-preference' });
      await serveProduct(page, lang);
      await page.addInitScript((label) => {
        Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
          enumerateDevices: async () => [
            { kind: 'audioinput', deviceId: 'short', label: 'USB' },
            { kind: 'audioinput', deviceId: 'long', label },
          ],
        } });
      }, longName);
      await open(page, '/settings/microphone', { lang, theme });
      const trigger = page.getByRole('button', { name: copy.chooseInput });
      await expect(trigger).toBeVisible();
      const geometry = () => trigger.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const parent = element.parentElement!.getBoundingClientRect();
        return { width: rect.width, height: rect.height, left: rect.left, right: rect.right,
          parentWidth: parent.width, parentLeft: parent.left, parentRight: parent.right };
      });
      const initial = await geometry();
      expect(initial.width).toBeLessThan(initial.parentWidth - 80);
      expect(initial.left).toBeCloseTo(initial.parentLeft, 0);
      // Desktop actions share the input control's left edge, below the test status.
      const start = page.getByRole('button', { name: copy.startTest, exact: true });
      const startBox = await start.boundingBox();
      expect(startBox!.x).toBeCloseTo(initial.left, 0);
      const statusBox = await page.getByText(copy.notTested, { exact: true }).boundingBox();
      expect(startBox!.y).toBeGreaterThan(statusBox!.y + statusBox!.height);
      await trigger.hover();
      await page.mouse.down();
      // Wait through the shared Button press animation so a delayed scale cannot pass.
      await page.waitForTimeout(350);
      const pressed = await geometry();
      expect(pressed.width).toBeCloseTo(initial.width, 1);
      expect(pressed.height).toBeCloseTo(initial.height, 1);
      await page.mouse.up();
      await expect(page.getByRole('group', { name: copy.deviceList })).toBeVisible();
      await page.getByRole('button', { name: /^USB / }).click();
      await expect(trigger).toContainText('USB');
      expect((await geometry()).width).toBeLessThan(initial.width);
      await trigger.click();
      await page.getByRole('button', { name: new RegExp('^Studio USB microphone') }).click();
      await expect(trigger).toContainText(longName);
      await page.setViewportSize({ width: 720, height: 800 });
      const constrained = await geometry();
      expect(constrained.right).toBeLessThanOrEqual(constrained.parentRight + 1);
      const label = trigger.getByText(longName, { exact: true });
      expect(await label.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
      await expect(trigger.locator('svg').last()).toBeVisible();
      await trigger.click();
      await expect(page.getByRole('group', { name: copy.deviceList })).toBeVisible();
    });
  }
}
