import { writeFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import { openOnboarding, renderedPhase, serveProduct, setDocumentHidden } from './support';

/**
 * The loop at the speed it was authored at, with no clock control anywhere: a video of
 * one full 8.9s cycle plus a hide/show in the middle of it, and a sampled log of what
 * the page was actually drawing at each step. This is the moving evidence — every
 * capture in `geometry.spec.ts` is a settled still, and the two answer different
 * questions. It lives in its own file because recording video is a per-file option.
 */
test.use({ viewport: { width: 1200, height: 800 }, video: 'on' });

test('the story runs, suspends with the tab and resumes', async ({ page }, info) => {
  const denied = await serveProduct(page);
  await openOnboarding(page, { realTime: true });

  const samples: { at: number; hidden: boolean; phase: Awaited<ReturnType<typeof renderedPhase>> }[] = [];
  const started = Date.now();
  for (let step = 0; step < 14; step += 1) {
    if (step === 6) await setDocumentHidden(page, true);
    if (step === 9) await setDocumentHidden(page, false);
    const hidden = step >= 6 && step < 9;
    samples.push({ at: Date.now() - started, hidden, phase: await renderedPhase(page) });
    await page.waitForTimeout(800);
  }

  // The hidden samples are all the same frame: a background tab advances nothing.
  const dark = samples.filter((sample) => sample.hidden);
  for (const sample of dark) expect(sample.phase).toEqual(dark[0].phase);
  // Over a full loop every card is seen working and every card is seen complete, so the
  // recording really is of the whole cycle rather than one long phase.
  const states = samples.map((sample) => sample.phase.states);
  for (let card = 0; card < 3; card += 1) {
    expect(states.some((state) => state[card] === 'working')).toBe(true);
    expect(states.some((state) => state[card] === 'complete')).toBe(true);
  }
  // Written to the output directory rather than attached inline, so the log lands beside
  // the video as a file a reviewer can open without a Playwright HTML report.
  const log = info.outputPath('normal-speed-phases.json');
  await writeFile(log, `${JSON.stringify(samples, null, 2)}\n`);
  await info.attach('normal-speed-phases.json', { path: log, contentType: 'application/json' });
  expect(denied).toEqual([]);
});
