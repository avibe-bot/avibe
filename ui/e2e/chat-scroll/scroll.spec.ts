import { expect, test, type Page } from '../support/browserTest';

import type { LabFrame, LabWrite, ScrollLab } from './recorder';

// The shipped ChatPage on a long conversation of mixed rows (fixture.tsx). Each
// test drives real input where the engine offers it (touch in Chromium, the
// wheel in WebKit), records every frame and every programmatic scroll of the
// transcript (recorder.ts), and holds one way the transcript used to move
// under the reader.

const transcript = (page: Page) => page.getByTestId('chat-transcript');
const IMAGE = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="420"><rect width="640" height="420" fill="#8ab"/></svg>';

async function open(page: Page, query: string, { imageDelay = 50, imagesHeldUntil }: { imageDelay?: number; imagesHeldUntil?: Promise<void> } = {}) {
  // No request from this page may reach a running Avibe service.
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  // Chart images arrive a moment after they are asked for, unmeasured, as a
  // fresh page's images do; or not until the test releases them.
  await page.route('**/lab-media/**', async (route) => {
    await (imagesHeldUntil ?? new Promise((resolve) => setTimeout(resolve, imageDelay)));
    await route.fulfill({ status: 200, contentType: 'image/svg+xml', body: IMAGE }).catch(() => {});
  });
  await page.goto(`/e2e/chat-scroll/fixture.html?${query}`);
  await expect(transcript(page).locator('[data-message-id]').first()).toBeVisible();
  await expect.poll(() => transcript(page).evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(1);
  await page.waitForTimeout(300);
}

const loads = (page: Page) => page.evaluate(() => Number(document.body.dataset.loads ?? 0));
const fromBottom = (page: Page) => transcript(page).evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
const setTop = (page: Page, top: number) => transcript(page).evaluate((el, value) => { el.scrollTop = value; }, top);
const nextFrames = (page: Page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

async function record(page: Page) {
  await page.evaluate(() => { window.__scrollLab.pinRef(0.4); window.__scrollLab.start(); });
}
async function stop(page: Page): Promise<{ frames: LabFrame[]; writes: LabWrite[] }> {
  await page.evaluate(() => window.__scrollLab.stop());
  const writes = await page.evaluate(() => window.__scrollLab.resolveSites());
  return { frames: await page.evaluate(() => window.__scrollLab.frames), writes };
}
const restoreWrites = (writes: LabWrite[]) => writes.filter((write) => write.site?.startsWith('restoreScrollPosition'));

/** Upward input the engine supports: a touch fling in Chromium, a wheel burst in WebKit. */
async function flingUp(page: Page, distance: number, browserName: string) {
  const box = (await transcript(page).boundingBox())!;
  if (browserName === 'chromium') {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.synthesizeScrollGesture', {
      x: Math.round(box.x + box.width / 2),
      y: Math.round(box.y + box.height * 0.3),
      yDistance: distance,
      speed: 4000,
      gestureSourceType: 'touch',
      preventFling: false,
    });
    await cdp.detach();
  } else {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    for (let step = 0; step < 10; step += 1) {
      await page.mouse.wheel(0, -distance / 10);
      await page.waitForTimeout(16);
    }
  }
}

test('an upward fling gets the older page before it reaches the top, and starting the load moves nothing', async ({ page, browserName }, testInfo) => {
  await open(page, 'delay=300');
  await setTop(page, 2500);
  await page.waitForTimeout(200);
  await record(page);
  for (let fling = 0; fling < 3 && (await loads(page)) === 0; fling += 1) await flingUp(page, 900, browserName);
  await expect.poll(() => loads(page)).toBe(1);
  await expect(transcript(page).locator('[data-message-id]').first()).toHaveAttribute('data-message-id', 'msg_00301');
  await page.waitForTimeout(400);
  const { frames, writes } = await stop(page);
  await testInfo.attach('trace.json', { body: JSON.stringify({ frames, writes }), contentType: 'application/json' });

  // The reader never sat at the hard top waiting for the page.
  expect(frames.filter((frame) => frame.top <= 0 && frame.loading)).toHaveLength(0);
  // The only write is the one that holds the reader as the page lands, made
  // while they still had history above them; none when the load started.
  const restores = restoreWrites(writes);
  expect(restores.filter((write) => write.to - write.from > 5000)).toHaveLength(1);
  expect(restores.filter((write) => write.to - write.from < 100 && write.to > write.from + 40)).toHaveLength(0);
  expect(restores[0].from).toBeGreaterThan(200);
});

test('an older page goes in only once the reader comes to rest, and holds their place', async ({ page }) => {
  // iOS takes back a scroll write made during momentum or the top bounce
  // (measured on iOS 27), which would drop the reader on the top of the page that
  // just landed. So the page waits for rest, however early it arrived.
  await open(page, 'delay=50&images=0');
  // Just outside the prefetch band (eight screens), so the request comes mid-fling.
  await transcript(page).evaluate((el) => { el.scrollTop = el.clientHeight * 8 + 300; });
  await page.waitForTimeout(300);
  expect(await loads(page)).toBe(0);
  const first = () => transcript(page).locator('[data-message-id]').first().getAttribute('data-message-id');
  const head = await first();
  // Keep the transcript moving, a few pixels a frame, as a long fling does.
  await transcript(page).evaluate((el) => {
    const fling = { stop: false };
    (window as unknown as { fling: typeof fling }).fling = fling;
    const step = () => {
      if (fling.stop) return;
      el.scrollTop -= 6;
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
  await expect.poll(() => loads(page)).toBe(1);
  // The page is in hand, but nothing goes in while the transcript moves.
  await page.waitForTimeout(600);
  expect(await first()).toBe(head);
  await page.evaluate(() => window.__scrollLab.pinRef(0));
  await page.evaluate(() => { (window as unknown as { fling: { stop: boolean } }).fling.stop = true; });
  await nextFrames(page);
  const reference = await page.evaluate(() => window.__scrollLab.refTop());
  await expect.poll(first).not.toBe(head);
  // Rows that go in above the reader are history: nothing enters or glides.
  const moving = await transcript(page).evaluate((el) => {
    const rows = [...el.querySelectorAll('[data-message-id]')];
    return [rows[0].parentElement!, ...rows].filter((node) => node.getAnimations().length > 0).length;
  });
  expect(moving).toBe(0);
  await nextFrames(page);
  const after = await page.evaluate(() => window.__scrollLab.refTop());
  expect(Math.abs(after! - reference!)).toBeLessThanOrEqual(1);
});

test('the reading position holds within a pixel through every older page, the retained-window cap, and the images that arrive with them', async ({ page }) => {
  await open(page, 'delay=800');
  for (let page_ = 0; page_ < 7; page_ += 1) {
    const before = await loads(page);
    await setTop(page, 0);
    await nextFrames(page);
    // Images already on screen finish first: one loading in view moves what is
    // below it, and the reader sees it load. Only the new page is above them.
    await expect.poll(() => transcript(page).evaluate((el) => {
      const box = el.getBoundingClientRect();
      return [...el.querySelectorAll('img')].filter((img) => {
        const rect = img.getBoundingClientRect();
        return rect.bottom > box.top && rect.top < box.bottom;
      }).every((img) => img.complete);
    })).toBe(true);
    const rows = await transcript(page).locator('[data-message-id]').count();
    // The reader's line: the first row that starts on screen. (A row below it
    // can still reflow by a fraction of a pixel when the page above changes
    // what it follows; that is content changing in view, not the scroll.)
    await page.evaluate(() => window.__scrollLab.pinRef(0));
    const reference = await page.evaluate(() => window.__scrollLab.refTop());
    await expect.poll(() => loads(page)).toBe(before + 1);
    await expect(page.getByRole('status', { name: /older|更早/ })).toHaveCount(0);
    // Let the new page's images arrive and lay out.
    await page.waitForTimeout(300);
    const after = await page.evaluate(() => window.__scrollLab.refTop());
    expect(await transcript(page).locator('[data-message-id]').count()).toBeGreaterThan(Math.min(rows, 299));
    expect(Math.abs(after! - reference!), `page ${page_ + 1}`).toBeLessThanOrEqual(1);
  }
  await expect(transcript(page).locator('[data-message-id]')).toHaveCount(300);
});

test('an image that loads in the row across the top edge does not move what the reader sees', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  // msg_00200, the newest row of the page that lands, has a chart; held.
  await open(page, 'delay=0&count=250', { imagesHeldUntil: held });
  await setTop(page, 0);
  await expect(transcript(page).locator('[data-message-id]')).toHaveCount(100);
  // The page landed above the reader: the row the reader was on keeps its
  // place, and the bottom of msg_00200 now shows above it, across the top edge.
  await nextFrames(page);
  const across = await transcript(page).evaluate((el) => {
    const row = el.querySelector<HTMLElement>('[data-message-id="msg_00200"]')!.getBoundingClientRect();
    const top = el.getBoundingClientRect().top;
    return row.top < top && row.bottom > top;
  });
  expect(across).toBe(true);
  await page.evaluate(() => window.__scrollLab.pinRef(0));
  const reference = await page.evaluate(() => window.__scrollLab.refTop());
  release();
  await page.waitForTimeout(500);
  const after = await page.evaluate(() => window.__scrollLab.refTop());
  expect(Math.abs(after! - reference!)).toBeLessThanOrEqual(1);
});

test('an image that lands above the reader while they scroll is compensated, not taken for where they are', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await open(page, 'delay=0&count=60', { imagesHeldUntil: held });
  await setTop(page, 0);
  await expect(transcript(page).locator('[data-message-id]')).toHaveCount(60);
  // Stand with a held image's row two screens above the viewport.
  await transcript(page).evaluate((el) => {
    const image = [...el.querySelectorAll('img')].find((img) => !img.complete || img.naturalHeight === 0)!;
    const row = image.closest<HTMLElement>('[data-message-id]')!;
    el.scrollTop += row.getBoundingClientRect().bottom - el.getBoundingClientRect().top + el.clientHeight * 2;
  });
  await page.waitForTimeout(100);
  await page.evaluate(() => window.__scrollLab.pinRef(0));
  const reference = await page.evaluate(() => window.__scrollLab.refTop());
  // Scroll up a few pixels every frame, as a slow fling does: every frame then
  // has a scroll event, delivered before the ResizeObserver hears of the image.
  await transcript(page).evaluate((el) => {
    const fling = { stop: false, moved: 0 };
    (window as unknown as { fling: typeof fling }).fling = fling;
    const step = () => {
      if (fling.stop) return;
      const before = el.scrollTop;
      el.scrollTop = before - 3;
      fling.moved += before - el.scrollTop;
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
  await page.waitForTimeout(150);
  release();
  await page.waitForTimeout(400);
  const moved = await page.evaluate(() => {
    const fling = (window as unknown as { fling: { stop: boolean; moved: number } }).fling;
    fling.stop = true;
    return fling.moved;
  });
  await nextFrames(page);
  const after = await page.evaluate(() => window.__scrollLab.refTop());
  // The reference moved down by exactly what the reader scrolled, and no more.
  expect(Math.abs(after! - reference! - moved)).toBeLessThanOrEqual(1);
});

test('the reader\'s own scroll is not written back when a row lands before its scroll event', async ({ page }) => {
  // No images: a late one above the reader is a real shift, compensated by design.
  // No older history either (the bootstrap holds all 50 rows): 4000 px from the top
  // is inside the older-page prefetch band, and a page prepended above the reader is
  // compensated by design too. Whether it went in before, during or after the
  // recording depended on the runner's timing.
  await open(page, 'delay=0&images=0&count=50');
  await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>('[data-testid="chat-transcript"]')!;
    const block = (event: Event) => event.stopImmediatePropagation();
    const release = () => {
      el.removeEventListener('scroll', block, true);
      el.dispatchEvent(new Event('scroll'));
    };
    // Hold the initial reader move too: the product must see the position
    // before its scroll event, not a later move after it already unpinned.
    el.addEventListener('scroll', block, true);
    (window.__scrollLab as ScrollLab & { releaseScrollEvent?: () => void }).releaseScrollEvent = release;
    el.scrollTop = 4000;
  });
  await page.waitForTimeout(200);
  await record(page);
  // Asynchronous scrolling (iOS, WebKit) moves the position first and delivers
  // its scroll event at the next frame; a live row can commit in between.
  for (let step = 0; step < 4; step += 1) {
    await page.evaluate(() => {
      const el = document.querySelector<HTMLElement>('[data-testid="chat-transcript"]')!;
      el.scrollTop -= 120;
      window.__scrollLab.push();
    });
    await page.waitForTimeout(120);
  }
  await page.evaluate(() => {
    const lab = window.__scrollLab as ScrollLab & { releaseScrollEvent?: () => void };
    lab.releaseScrollEvent?.();
    delete lab.releaseScrollEvent;
  });
  const { writes } = await stop(page);
  expect(await loads(page)).toBe(0);
  expect(restoreWrites(writes)).toEqual([]);
});

test('a reader starting to scroll up is not pulled back to a tail that is still streaming', async ({ page, browserName }) => {
  await open(page, 'delay=0');
  const box = (await transcript(page).boundingBox())!;
  const x = Math.round(box.x + box.width / 2);
  let y = Math.round(box.y + box.height * 0.3);
  const distances: number[] = [];
  if (browserName === 'chromium') {
    // A finger moving down in small steps while rows stream in.
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
    for (let step = 0; step < 12; step += 1) {
      y += 6;
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y }] });
      await nextFrames(page);
      if (step % 3 === 2) {
        await page.evaluate(() => window.__scrollLab.push('A row streaming in.'));
        await nextFrames(page);
        distances.push(await fromBottom(page));
      }
    }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await cdp.detach();
  } else {
    await page.mouse.move(x, y);
    for (let step = 0; step < 4; step += 1) {
      await page.mouse.wheel(0, -12);
      await nextFrames(page);
      await page.evaluate(() => window.__scrollLab.push('A row streaming in.'));
      await nextFrames(page);
      distances.push(await fromBottom(page));
    }
  }
  // Every row after the reader started moving up stays below them.
  expect(distances.slice(1).every((distance) => distance > 12), JSON.stringify(distances)).toBe(true);
});

test('a Harness group opened across the top edge opens where the reader touched it', async ({ page }) => {
  await open(page, 'delay=0&images=0');
  const group = transcript(page).locator('[data-harness-group]').last();
  // Stand with the collapsed group across the top edge, its card still on screen.
  await group.evaluate((head) => {
    const el = head.closest<HTMLElement>('[data-testid="chat-transcript"]')!;
    el.scrollTop += head.getBoundingClientRect().top - el.getBoundingClientRect().top + 30;
  });
  await page.waitForTimeout(100);
  const first = (await group.getAttribute('data-harness-members'))!.split(' ')[0];
  const before = await group.evaluate((head) => head.getBoundingClientRect().top);
  const card = (await group.getByRole('button').boundingBox())!;
  await page.mouse.click(card.x + 60, card.y + card.height - 20);
  const member = transcript(page).locator(`[data-message-id="${first}"]`);
  await expect(member).toHaveCount(1);
  await nextFrames(page);
  // The group held still, and what it opened is on screen below it.
  expect(Math.abs(await group.evaluate((head) => head.getBoundingClientRect().top) - before)).toBeLessThanOrEqual(1);
  const onScreen = await member.evaluate((row) => {
    const box = row.closest<HTMLElement>('[data-testid="chat-transcript"]')!.getBoundingClientRect();
    const rect = row.getBoundingClientRect();
    return rect.top >= box.top && rect.top < box.bottom;
  });
  expect(onScreen).toBe(true);
});

test('following the tail, opening something holds it where it was tapped and stops following; a plain tap does not', async ({ page }) => {
  // count=392 puts the last execution-process chip on screen at the bottom.
  await open(page, 'delay=0&images=0&activity=1&count=392');
  const bottomGap = () => transcript(page).evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);
  // The transcript item holding the last chip (it re-mounts as it opens), and
  // its top: what holds still. The conversation's glide must be over first.
  const settled = () => transcript(page).evaluate((el) => el.querySelector('[data-message-id]')!.parentElement!.getAnimations().length === 0);
  const itemTop = () => transcript(page).evaluate((el) => {
    const column = el.querySelector('[data-message-id]')!.parentElement!;
    const chip = [...el.querySelectorAll('[aria-expanded]')].at(-1)!;
    return [...column.children].find((child) => child.contains(chip))!.getBoundingClientRect().top;
  });
  expect(await bottomGap()).toBeLessThan(1);

  // A plain tap on a message keeps following: a row streaming in is followed.
  const text = transcript(page).locator('[data-message-id]').last();
  const textBox = (await text.boundingBox())!;
  await page.mouse.click(textBox.x + 20, textBox.y + Math.min(20, textBox.height / 2));
  await page.evaluate(() => window.__scrollLab.push('A row streaming in.'));
  await expect.poll(bottomGap).toBeLessThan(1);
  await expect.poll(settled).toBe(true);

  // Opening the chip: its item holds still, and the next row does not pull it away.
  const chip = transcript(page).locator('[aria-expanded]').last();
  const box = (await chip.boundingBox())!;
  const before = await itemTop();
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(transcript(page).locator('[aria-expanded="true"]')).toHaveCount(1);
  await nextFrames(page);
  expect(Math.abs(await itemTop() - before)).toBeLessThanOrEqual(1);
  await page.evaluate(() => window.__scrollLab.push('Another row streaming in.'));
  await page.waitForTimeout(400);
  expect(Math.abs(await itemTop() - before)).toBeLessThanOrEqual(1);
  expect(await bottomGap()).toBeGreaterThan(1);
});

// The way back to the latest message (the down-arrow), recorded frame by frame.
async function glideFrames(page: Page, act: () => Promise<void>) {
  await page.evaluate(() => window.__scrollLab.start());
  await act();
  await page.waitForTimeout(700);
  await page.evaluate(() => window.__scrollLab.stop());
  return page.evaluate(() => window.__scrollLab.frames.map((frame) => [frame.t, frame.top, frame.height - frame.client] as const));
}
const toLatest = (page: Page) => page.getByRole('button', { name: /Scroll to latest|回到最新/ });
const gapToBottom = (page: Page) => transcript(page).evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);
// The reader's own scroll, as far from a write of the scroller's as a finger
// is: a scroll's end within 100 ms of one is taken for that write's own.
const standAt = async (page: Page, screensUp: number) => {
  await page.waitForTimeout(200);
  await transcript(page).evaluate((el, screens) => { el.scrollTop = el.scrollHeight - el.clientHeight * (1 + screens); }, screensUp);
  await page.waitForTimeout(400);
};

test('an explicit jump to latest clears an old anchor takeback before a reader scroll', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await open(page, 'delay=0&count=60', { imagesHeldUntil: held });
  await setTop(page, 0);
  await expect(transcript(page).locator('[data-message-id]')).toHaveCount(60);
  await transcript(page).evaluate((el) => {
    const image = [...el.querySelectorAll('img')].find((img) => !img.complete || img.naturalHeight === 0)!;
    const row = image.closest<HTMLElement>('[data-message-id]')!;
    el.scrollTop += row.getBoundingClientRect().bottom - el.getBoundingClientRect().top + el.clientHeight * 2;
  });
  await page.waitForTimeout(100);
  await transcript(page).evaluate((el) => {
    const fling = { stop: false };
    (window as unknown as { takebackFling: typeof fling }).takebackFling = fling;
    const step = () => {
      if (fling.stop || el.scrollTop <= 0) return;
      el.scrollTop -= 3;
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
  await page.waitForTimeout(150);
  release();
  await page.waitForTimeout(400);
  await page.evaluate(() => { (window as unknown as { takebackFling: { stop: boolean } }).takebackFling.stop = true; });
  await nextFrames(page);

  await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>('[data-testid="chat-transcript"]')!;
    const block = (event: Event) => event.stopImmediatePropagation();
    el.addEventListener('scroll', block, true);
    (window.__scrollLab as ScrollLab & { releaseScrollEvent?: () => void }).releaseScrollEvent = () => {
      el.removeEventListener('scroll', block, true);
      el.dispatchEvent(new Event('scroll'));
    };
  });
  await toLatest(page).click();
  await expect.poll(() => gapToBottom(page)).toBeLessThan(1);
  await page.evaluate(() => window.__scrollLab.start());
  await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>('[data-testid="chat-transcript"]')!;
    el.scrollTop -= 120;
    window.__scrollLab.push('A row lands while the reader scroll event is queued.');
  });
  await page.waitForTimeout(200);
  await page.evaluate(() => {
    const lab = window.__scrollLab as ScrollLab & { releaseScrollEvent?: () => void };
    lab.releaseScrollEvent?.();
    delete lab.releaseScrollEvent;
  });
  const { writes } = await stop(page);
  expect(restoreWrites(writes)).toEqual([]);
});

test('the way back to the latest message is a quick glide that slows as it lands, then follows the tail', async ({ page }) => {
  // All 50 rows are in the first window: nothing older to load.
  await open(page, 'delay=0&images=0&count=50');
  await standAt(page, 2);
  const frames = await glideFrames(page, () => toLatest(page).click());
  const startTop = frames[0][1];
  const bottom = frames.at(-1)![2];
  expect(frames.at(-1)![1]).toBeGreaterThanOrEqual(bottom - 1);
  const between = frames.filter(([, top]) => top > startTop + 1 && top < bottom - 1);
  expect(between.length).toBeGreaterThanOrEqual(5);
  const firstMove = frames.find(([, top]) => top > startTop + 1)![0];
  const landed = frames.find(([, top]) => top >= bottom - 1)![0];
  expect(landed - firstMove).toBeGreaterThan(150);
  expect(landed - firstMove).toBeLessThan(500);
  // It slows as it lands: the last third of the way takes longer than the first.
  const at = (fraction: number) => frames.find(([, top]) => top >= startTop + (bottom - startTop) * fraction)![0];
  expect(landed - at(2 / 3)).toBeGreaterThan(at(1 / 3) - firstMove);
  // At the bottom it follows the tail again.
  await page.evaluate(() => window.__scrollLab.push('A row streaming in.'));
  await expect.poll(() => gapToBottom(page)).toBeLessThan(1);

  // From far up it cuts to a screen above the bottom first, then glides.
  await standAt(page, 8);
  const far = await glideFrames(page, () => toLatest(page).click());
  const farStart = far[0][1];
  const moved = far.filter(([, top]) => top > farStart + 1);
  const client = await transcript(page).evaluate((el) => el.clientHeight);
  expect(moved[0][1]).toBeGreaterThanOrEqual(far.at(-1)![2] - client - 2);
  expect(moved.filter(([, top, end]) => top < end - 1).length).toBeGreaterThanOrEqual(5);
  expect(await gapToBottom(page)).toBeLessThan(1);
});

test('the glide to the latest message gives way to the reader, and is a cut under reduced motion', async ({ page }) => {
  await open(page, 'delay=0&images=0&count=50');
  await standAt(page, 2);
  await toLatest(page).click();
  await page.waitForTimeout(90);
  // Shift + wheel down would also count; a wheel up is the reader taking over.
  const box = (await transcript(page).boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -40);
  await page.waitForTimeout(150);
  const stoppedAt = await transcript(page).evaluate((el) => el.scrollTop);
  await page.waitForTimeout(400);
  expect(await transcript(page).evaluate((el) => el.scrollTop)).toBeLessThanOrEqual(stoppedAt + 1);
  expect(await gapToBottom(page)).toBeGreaterThan(1);

  await page.emulateMedia({ reducedMotion: 'reduce' });
  await standAt(page, 2);
  const frames = await glideFrames(page, () => toLatest(page).click());
  const startTop = frames[0][1];
  const bottom = frames.at(-1)![2];
  expect(frames.filter(([, top]) => top > startTop + 1 && top < bottom - 1)).toHaveLength(0);
  expect(await gapToBottom(page)).toBeLessThan(1);
});

test('older rows appear at once after a chat opened on its skeleton', async ({ page }) => {
  await open(page, 'reader=local&images=0&inbox=1&boot=600&delay=100');
  // The live window replaced the skeleton (which holds one row, the Inbox's).
  await expect(transcript(page).locator('[data-message-id]').first()).toHaveAttribute('data-message-id', 'msg_00351');
  await page.waitForTimeout(1200);
  const opacity = await transcript(page).evaluate(async (el) => {
    el.scrollTop = 0;
    for (;;) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      const first = el.querySelector<HTMLElement>('[data-message-id]')!;
      if (first.dataset.messageId !== 'msg_00351') return getComputedStyle(first).opacity;
    }
  });
  expect(opacity).toBe('1');
});
