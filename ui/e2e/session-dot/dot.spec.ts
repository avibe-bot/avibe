import { expect, test, type Locator, type Page } from '@playwright/test';

/**
 * The two claims the owner made, measured in a browser rather than inferred
 * from class names: a running session's dot keeps pulsing, and selecting a row
 * does not move its dot or its name.
 *
 * The alignment half needs a browser specifically. At the base commit the row
 * carried both an unconditional 26px left padding and a selected-only 24px one
 * meant to pay for the 2px accent border. Two utilities of equal specificity are
 * resolved by their order in the generated stylesheet, not by the order of the
 * class string, so reading the source cannot tell you which one applied — only
 * the computed box can.
 */

const RUNNING = '实例描述字段：实现';
const RUNNING_SIBLING = '第二个运行中的会话';
const IDLE = '修复组织成员 Model';
const FAILED = '失败的会话';
const READONLY_RUNNING = '只读运行中的会话';
const WRITABLE_PROJECT = '可写项目';
const READONLY_PROJECT = '只读项目';

/** Whichever of the two session surfaces this project's viewport renders. */
const surface = (page: Page) =>
  page.getByTestId('desktop-tree').or(page.getByTestId('mobile-tree')).filter({ visible: true });

/** The row's own navigation control — the dot and the name live inside it. */
const rowButton = (page: Page, title: string) =>
  surface(page).getByRole('button').filter({ hasText: title }).first();

const dotOf = (button: Locator) => button.locator('span').first();
const labelOf = (button: Locator, title: string) => button.getByText(title, { exact: true });

/** The desktop row container: the element that carries the selected accent. */
const rowOf = (page: Page, title: string) =>
  surface(page).locator('div:has(> button > span[title])').filter({ hasText: title }).first();

const expand = async (page: Page, project: string) => {
  await surface(page).getByText(project, { exact: true }).click();
};

const boxOf = async (locator: Locator, what: string) => {
  const box = await locator.boundingBox();
  expect(box, `${what} should be laid out`).not.toBeNull();
  return box!;
};

/** What the user sees line up (or not): the dot's centre and the name's left edge. */
const geometry = async (page: Page, title: string) => {
  const button = rowButton(page, title);
  const dot = await boxOf(dotOf(button), `${title} dot`);
  const label = await boxOf(labelOf(button, title), `${title} name`);
  return { dotCentreX: dot.x + dot.width / 2, nameLeft: label.x };
};

/**
 * Reads the dot's real CSS animation, then walks its timeline by hand to sample
 * the whole cycle — including a second and third period, which is what "keeps
 * blinking" and "never disappears" actually mean.
 */
const motionOf = (dot: Locator) =>
  dot.evaluate((el) => {
    const animations = el.getAnimations().filter((a): a is CSSAnimation => 'animationName' in a);
    if (animations.length === 0) return { animated: false as const };
    const [animation] = animations;
    const timing = animation.effect?.getTiming();
    animation.pause();
    const opacityAt: Record<string, number> = {};
    for (const ms of [0, 250, 500, 1000, 1500, 2000, 2500, 3000, 4000]) {
      animation.currentTime = ms;
      opacityAt[String(ms)] = Number(getComputedStyle(el).opacity);
    }
    animation.play();
    return {
      animated: true as const,
      name: animation.animationName,
      durationMs: typeof timing?.duration === 'number' ? timing.duration : null,
      endless: timing?.iterations === Infinity,
      opacityAt,
    };
  });

/** Untouched wall-clock evidence that the declared animation is really running. */
const opacityOf = (dot: Locator) => dot.evaluate((el) => Number(getComputedStyle(el).opacity));

const expectGentleTwoSecondPulse = (motion: Awaited<ReturnType<typeof motionOf>>) => {
  expect(motion.animated, 'the running dot should carry a CSS animation').toBe(true);
  if (!motion.animated) return;
  expect(motion.name).toBe('pulse');
  expect(motion.durationMs).toBe(2000);
  expect(motion.endless, 'the pulse should not stop on its own').toBe(true);
  // Cycle boundaries: full at the ends, half-lit at the middle, and the same
  // again one and two periods later.
  expect(motion.opacityAt['0']).toBeCloseTo(1, 2);
  expect(motion.opacityAt['1000']).toBeCloseTo(0.5, 2);
  expect(motion.opacityAt['2000']).toBeCloseTo(1, 2);
  expect(motion.opacityAt['3000']).toBeCloseTo(0.5, 2);
  expect(motion.opacityAt['4000']).toBeCloseTo(1, 2);
  // Gentle: the dot dims, it never goes out.
  const samples = Object.values(motion.opacityAt);
  expect(Math.min(...samples)).toBeGreaterThanOrEqual(0.5);
  expect(Math.max(...samples)).toBeLessThanOrEqual(1);
};

test.beforeEach(async ({ page }) => {
  await page.goto('/e2e/session-dot/fixture.html');
  await expect(surface(page).getByText(WRITABLE_PROJECT, { exact: true })).toBeVisible();
  await expand(page, WRITABLE_PROJECT);
  await expect(surface(page).getByText(RUNNING, { exact: true })).toBeVisible();
});

test('a running session keeps pulsing and an idle or failed one stays still', async ({ page }) => {
  expectGentleTwoSecondPulse(await motionOf(dotOf(rowButton(page, RUNNING))));

  // The same claim without touching the timeline: left alone, the dot's opacity
  // is somewhere else half a period later.
  const first = await opacityOf(dotOf(rowButton(page, RUNNING))); // resumed at ~0
  await page.waitForTimeout(700);
  const second = await opacityOf(dotOf(rowButton(page, RUNNING)));
  expect(Math.abs(second - first), 'the dot should visibly change on its own').toBeGreaterThan(0.05);

  for (const still of [IDLE, FAILED]) {
    expect((await motionOf(dotOf(rowButton(page, still)))).animated, `${still} should not pulse`).toBe(false);
  }
});

test('the pulse follows the runtime status alone, not selection or the surface', async ({ page }) => {
  // Selecting the row must neither start nor stop the motion.
  await rowButton(page, IDLE).click();
  expect((await motionOf(dotOf(rowButton(page, IDLE)))).animated, 'a selected idle row').toBe(false);
  expectGentleTwoSecondPulse(await motionOf(dotOf(rowButton(page, RUNNING))));

  await rowButton(page, RUNNING).click();
  expectGentleTwoSecondPulse(await motionOf(dotOf(rowButton(page, RUNNING))));

  // A read-only project's rows are the same dot.
  await expand(page, READONLY_PROJECT);
  await expect(surface(page).getByText(READONLY_RUNNING, { exact: true })).toBeVisible();
  expectGentleTwoSecondPulse(await motionOf(dotOf(rowButton(page, READONLY_RUNNING))));
});

test('a live status change starts and stops the pulse without a reload', async ({ page }) => {
  const dot = dotOf(rowButton(page, RUNNING));
  expect((await motionOf(dot)).animated).toBe(true);

  await page.evaluate((id) => window.setSessionStatus(id, 'idle'), 'writable-running');
  await expect.poll(async () => (await motionOf(dot)).animated, {
    message: 'the pulse should stop when the session stops running',
  }).toBe(false);

  await page.evaluate((id) => window.setSessionStatus(id, 'running'), 'writable-idle');
  const revived = dotOf(rowButton(page, IDLE));
  await expect.poll(async () => (await motionOf(revived)).animated, {
    message: 'the pulse should start when the session starts running',
  }).toBe(true);
  expectGentleTwoSecondPulse(await motionOf(revived));
});

test('reduced motion keeps the static status dot', async ({ page }) => {
  const dot = dotOf(rowButton(page, RUNNING));
  expect((await motionOf(dot)).animated, 'the dot pulses before the preference changes').toBe(true);
  const lit = await dot.evaluate((el) => getComputedStyle(el).backgroundColor);

  await page.emulateMedia({ reducedMotion: 'reduce' });

  expect((await motionOf(dot)).animated, 'reduced motion should not animate').toBe(false);
  expect(await opacityOf(dot), 'the static dot stays fully lit').toBeCloseTo(1, 2);
  expect((await boxOf(dot, 'reduced-motion dot')).width).toBeGreaterThan(0);
  // Same green, same size: reduced motion drops the animation, not the status.
  await expect(dot).toHaveCSS('background-color', lit);
  expect(lit).not.toBe('rgba(0, 0, 0, 0)');
});

test.describe('desktop row alignment', () => {
  test.skip(({ isMobile }) => Boolean(isMobile), 'the selected-row accent is a desktop-tree affordance');

  test('selecting a row leaves its dot and name where they were', async ({ page }) => {
    // The localized status description the dot carries survives the change.
    await expect(dotOf(rowButton(page, RUNNING))).toHaveAttribute('title', 'Running');
    await expect(dotOf(rowButton(page, IDLE))).toHaveAttribute('title', 'Idle');
    await expect(dotOf(rowButton(page, FAILED))).toHaveAttribute('title', 'Last turn failed');

    const before = await geometry(page, RUNNING);
    const sibling = await geometry(page, RUNNING_SIBLING);
    expect(before.dotCentreX).toBeCloseTo(sibling.dotCentreX, 1);
    expect(before.nameLeft).toBeCloseTo(sibling.nameLeft, 1);

    await rowButton(page, RUNNING).click();
    // The row really is selected: the accent it reserves space for is now lit.
    await expect(rowOf(page, RUNNING)).not.toHaveCSS('border-left-color', 'rgba(0, 0, 0, 0)');
    await expect(rowOf(page, RUNNING)).toHaveCSS('border-left-width', '2px');

    const selected = await geometry(page, RUNNING);
    expect(selected.dotCentreX, 'the selected dot must not shift right').toBeCloseTo(before.dotCentreX, 1);
    expect(selected.nameLeft, 'the selected name must not shift right').toBeCloseTo(before.nameLeft, 1);
    // ... and against the row beside it, which is what the user compares it to.
    expect(selected.dotCentreX).toBeCloseTo((await geometry(page, RUNNING_SIBLING)).dotCentreX, 1);
    expect(selected.nameLeft).toBeCloseTo((await geometry(page, RUNNING_SIBLING)).nameLeft, 1);
  });

  test('moving the selection does not move either row', async ({ page }) => {
    await rowButton(page, RUNNING).click();
    const firstSelected = await geometry(page, RUNNING);
    const firstUnselected = await geometry(page, IDLE);

    await rowButton(page, IDLE).click();
    await expect(rowOf(page, IDLE)).not.toHaveCSS('border-left-color', 'rgba(0, 0, 0, 0)');
    await expect(rowOf(page, RUNNING)).toHaveCSS('border-left-color', 'rgba(0, 0, 0, 0)');

    // Each row's geometry is the same as it was in the opposite selection state.
    expect((await geometry(page, RUNNING)).dotCentreX).toBeCloseTo(firstSelected.dotCentreX, 1);
    expect((await geometry(page, RUNNING)).nameLeft).toBeCloseTo(firstSelected.nameLeft, 1);
    expect((await geometry(page, IDLE)).dotCentreX).toBeCloseTo(firstUnselected.dotCentreX, 1);
    expect((await geometry(page, IDLE)).nameLeft).toBeCloseTo(firstUnselected.nameLeft, 1);
  });

  test('a read-only row and a row being renamed keep the same indent', async ({ page }) => {
    await expand(page, READONLY_PROJECT);
    await expect(surface(page).getByText(READONLY_RUNNING, { exact: true })).toBeVisible();

    const writable = await geometry(page, RUNNING);
    const readOnly = await geometry(page, READONLY_RUNNING);
    expect(readOnly.dotCentreX, 'a read-only row is indented like a writable one').toBeCloseTo(writable.dotCentreX, 1);
    expect(readOnly.nameLeft).toBeCloseTo(writable.nameLeft, 1);

    // The action rail only exists where metadata is writable, and that is a
    // right-hand affordance: it must not change the left indent either way.
    await expect(rowOf(page, RUNNING).getByRole('button', { name: 'Session actions' })).toBeAttached();
    await expect(rowOf(page, READONLY_RUNNING).getByRole('button', { name: 'Session actions' })).toHaveCount(0);

    // Inline rename replaces the row: the dot has to stay put while typing.
    const dotBefore = await boxOf(dotOf(rowButton(page, RUNNING)), 'dot before rename');
    await rowOf(page, RUNNING).click({ button: 'right' });
    await page.getByRole('group', { name: 'Session actions' }).getByRole('button', { name: 'Rename' }).click();
    const renameRow = surface(page).locator('div:has(> input)').first();
    await expect(renameRow.getByPlaceholder('Session name')).toBeVisible();
    const renameDot = await boxOf(renameRow.locator('span').first(), 'dot while renaming');
    expect(renameDot.x + renameDot.width / 2).toBeCloseTo(dotBefore.x + dotBefore.width / 2, 1);
    await page.keyboard.press('Escape');
  });
});
