import { expect, stubVoiceWorklet, test, type Page } from '../support/browserTest';

// support/browserTest's contract, in the engines the suites run: the composer's
// voice worklet loads nothing from before the first page script runs, in the
// context the base gives a test and in a context a spec makes itself, and
// `voiceWorklet: 'real'` keeps the browser's own. A page whose first inline
// script records what it found stands in for the composer, which loads the
// worklet once the page is idle.
const PROBE = '/e2e/chat-interactions/worklet-probe.html';
const PROBE_HTML = `<!doctype html><script>
  window.found = typeof AudioWorklet === 'undefined'
    ? 'absent'
    : String(AudioWorklet.prototype.addModule).includes('[native code]') ? 'native' : 'stub';
</script>`;

// On the suite's own origin (a context a spec makes has no baseURL), which is
// also a secure context, where AudioWorklet exists.
async function firstScriptFinds(page: Page, origin: string | undefined): Promise<string> {
  const url = new URL(PROBE, origin).href;
  await page.route(url, (route) => route.fulfill({ contentType: 'text/html', body: PROBE_HTML }));
  await page.goto(url);
  return page.evaluate(() => (window as unknown as { found: string }).found);
}

test('the base stubs the voice worklet before the first page script', async ({ page, baseURL }) => {
  expect(await firstScriptFinds(page, baseURL)).toBe('stub');
});

test('a context a spec makes takes the stub through stubVoiceWorklet', async ({ browser, baseURL }) => {
  const bare = await browser.newContext();
  const made = await browser.newContext();
  await stubVoiceWorklet(made);
  try {
    // The browser's own worklet, without it.
    expect(await firstScriptFinds(await bare.newPage(), baseURL)).toBe('native');
    expect(await firstScriptFinds(await made.newPage(), baseURL)).toBe('stub');
  } finally {
    await bare.close();
    await made.close();
  }
});

test.describe('a suite that records', () => {
  test.use({ voiceWorklet: 'real' });

  test('keeps the browser’s own worklet', async ({ page, baseURL }) => {
    expect(await firstScriptFinds(page, baseURL)).toBe('native');
  });
});
