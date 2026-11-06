import { expect, test, type Page } from '../support/browserTest';

// The take card's meter draws the microphone's real level (ui/design: mobile/main-bot-voice/
// d-recording, composer/hold-desktop/d-recording), in a real browser on a fake microphone that
// plays a steady tone. It also measures how long after the hold completes (the press reaching the
// gesture's hold time) the meter draws its first frame with a level: the first take on a fresh
// page, then the takes after it. Each latency is printed and kept as a test annotation.

type Probe = { down: number[]; card: number[]; loud: number[] };

declare global {
  interface Window {
    meterProbe: Probe;
  }
}

/** A bar above the meter's quiet breath (at most 0.17) is a heard level. */
const LOUD = 0.18;
const TAKES = 5;

test.beforeEach(async ({ page }) => {
  await page.addInitScript((loud) => {
    const probe: Probe = { down: [], card: [], loud: [] };
    window.meterProbe = probe;
    window.addEventListener('pointerdown', (event) => probe.down.push(event.timeStamp), true);
    new MutationObserver((records) => {
      const now = performance.now();
      for (const record of records) {
        if (record.type === 'childList') {
          for (const node of record.addedNodes) {
            if (node instanceof Element && (node.matches('[data-voice-meter="live"]') || node.querySelector('[data-voice-meter="live"]'))) {
              probe.card.push(now);
            }
          }
          continue;
        }
        const bar = record.target as HTMLElement;
        if (bar.parentElement?.dataset.voiceMeter !== 'live' || probe.loud.length >= probe.card.length) continue;
        const scale = Number(/scaleY\(([\d.]+)\)/.exec(bar.style.transform)?.[1] ?? 0);
        if (scale > loud) probe.loud.push(now);
      }
    }).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['style'] });
  }, LOUD);
});

const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];

/** Holds `TAKES` times, and returns how long after each hold completed the first frame with a
 *  level drew. */
async function measure(page: Page, holdMs: number, take: () => Promise<void>, sent: () => Promise<number>) {
  for (let index = 0; index < TAKES; index += 1) {
    await take();
    await expect.poll(sent).toBe(index + 1);
  }
  const probe = await page.evaluate(() => window.meterProbe);
  expect(probe.loud).toHaveLength(TAKES);
  const latency = probe.loud.map((at, index) => Math.round(at - (probe.down[index] + holdMs)));
  const opened = probe.card.map((at, index) => Math.round(at - probe.down[index]));
  return { latency, opened };
}

function report(name: string, result: { latency: number[]; opened: number[] }) {
  const [first, ...warm] = result.latency;
  const line = `${name}: card at ${result.opened.join('/')} ms after the press; first frame with a level ${first} ms after the hold completed on a fresh page, then ${warm.join('/')} ms (median ${median(warm)} ms)`;
  console.log(line);
  test.info().annotations.push({ type: 'meter latency', description: line });
  // A loose regression guard, not the measurement (CI machines are slower): a meter that waited
  // for the microphone's PCM chunks (250 ms each) would show a level later than this.
  expect(median(warm)).toBeLessThan(400);
}

test('the composer, held with a mouse (350 ms)', async ({ page }) => {
  await page.goto('/e2e/voice-shortcut/fixture.html');
  const box = page.locator('[data-hold-to-talk="ready"]');
  await expect(box).toBeVisible();
  const rect = (await box.boundingBox())!;
  const result = await measure(page, 350, async () => {
    await page.mouse.move(rect.x + rect.width * 0.4, rect.y + 22);
    await page.mouse.down();
    await page.waitForTimeout(1_200);
    await page.mouse.up();
  }, () => page.evaluate(() => window.voiceShortcutTest.spoken.length));
  report('desktop composer', result);
});

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("the tab bar's main Bot, held with a finger (200 ms)", async ({ page }) => {
    await page.goto('/e2e/voice-shortcut/fixture.html?surface=center');
    const center = page.getByRole('button', { name: 'Open Vibey' });
    await expect(center).toBeVisible();
    const box = (await center.boundingBox())!;
    const at = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    const result = await measure(page, 200, async () => {
      const cdp = await page.context().newCDPSession(page);
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ ...at, id: 1 }] });
      await page.waitForTimeout(1_200);
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await cdp.detach();
      // The sent toast stays a while; the next take starts after it is read.
      await page.waitForTimeout(300);
    }, () => page.evaluate(() => window.voiceShortcutTest.sent.length));
    report('phone main Bot', result);
  });
});
