import { expect, test } from '@playwright/test';

for (const theme of ['light', 'dark']) {
  for (const lang of ['en', 'zh']) {
    test(`collapsed bubble spacing and action copy (${theme}, ${lang})`, async ({ page }) => {
      await page.goto(`/e2e/pet-mini/fixture.html?scenario=personal&lang=${lang}&queued=1`);
      await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
      await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
      await expect(page.locator('.pet-mini-stage')).toHaveCSS('color', theme === 'dark' ? 'rgb(255, 255, 255)' : 'rgb(23, 23, 28)');
      await page.evaluate(async () => {
        await Promise.all(document.getAnimations()
          .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
          .map((animation) => animation.finished.catch(() => {})));
      });
      const gap = await page.evaluate(() =>
        document.querySelector('.pet-mini-reply-bubble')!.getBoundingClientRect().top
        - document.querySelector('.pet-mini-collapsed')!.getBoundingClientRect().bottom);
      expect(gap).toBeGreaterThanOrEqual(13);
      expect(gap).toBeLessThanOrEqual(15);
      await expect(page.locator('.pet-mini-reply-toggle')).toContainText(lang === 'zh' ? /已回复：\S/ : /Replied: \S/);
      await page.locator('.pet-mini-collapsed').click();
      const stop = page.locator('.pet-mini-stop').first();
      await expect(stop).toHaveAttribute('aria-label', lang === 'zh' ? /^停止：.+/ : /^Stop: .+/);
      await stop.click();
      await expect(stop).toHaveAttribute('aria-label', lang === 'zh' ? '停止？' : 'Stop?');
      expect(await page.evaluate(() => window.petFixtureStops)).toEqual([]);
    });
  }
}

test('an unmounted surface releases desktop input and a remount reports fresh regions', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html');
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.petLayoutHitRegions.length)).toBeGreaterThan(0);
  // Unmount during the reporter's motion window, as an authorization recheck
  // does. Queued animation/observer work must not reinstall invisible regions.
  await page.locator('.pet-mini-collapsed').click();
  await page.evaluate(() => window.setPetLayoutVisible(false));
  await expect(page.locator('.pet-mini-avatar-button')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => window.petLayoutHitRegions)).toEqual([]);
  await page.evaluate(() => new Promise<void>((resolve, reject) => {
    const until = performance.now() + 600;
    const check = () => {
      if (window.petLayoutHitRegions.length) reject(new Error('Unmounted pet reclaimed desktop input'));
      else if (performance.now() >= until) resolve();
      else requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
  }));
  await page.evaluate(() => window.setPetLayoutVisible(true));
  await expect(page.locator('.pet-mini-avatar-button')).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const avatar = document.querySelector('.pet-mini-avatar-button')!.getBoundingClientRect();
    const x = avatar.left + avatar.width / 2, y = avatar.top + avatar.height / 2;
    return window.petLayoutHitRegions.some((rect) =>
      x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height);
  })).toBe(true);
});

for (const main of [true, false]) {
  for (const layers of [1, 3, 5]) {
    test(`collapsed layers follow the front height (main=${main}, layers=${layers})`, async ({ page }) => {
      const errors: string[] = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await page.goto(`/e2e/pet-mini/fixture.html?main=${main}&layers=${layers}&queued=1`);
      const renderedLayers = Math.min(layers, 3);
      await expect(page.locator('.pet-mini-layer')).toHaveCount(renderedLayers);
      // Wait for the real CSS entrance before measuring its transformed bounds.
      await page.locator('.pet-mini-swap').evaluate(async (node) => {
        await Promise.all(node.getAnimations().map((animation) => animation.finished));
      });
      const bounds = await page.locator('.pet-mini-collapsed').evaluate((stack) => {
        const front = stack.querySelector('.pet-mini-front')!.getBoundingClientRect();
        const backs = Array.from(stack.querySelectorAll('.pet-mini-layer'), (layer) => layer.getBoundingClientRect().toJSON());
        return { stack: stack.getBoundingClientRect().toJSON(), front: front.toJSON(), backs };
      });
      expect(bounds.stack.height).toBeCloseTo(bounds.front.height + renderedLayers * 9, 1);
      expect(bounds.backs.at(-1)!.bottom - bounds.front.bottom).toBeCloseTo(renderedLayers * 9, 1);
      for (const [index, layer] of bounds.backs.entries()) {
        expect(layer.height).toBeCloseTo(bounds.front.height, 1);
        expect(layer.bottom - bounds.front.bottom).toBeCloseTo((index + 1) * 9, 1);
      }
      const avatar = await page.locator('.pet-mini-avatar-button').boundingBox();
      await page.locator('.pet-mini-collapsed').click();
      await expect(page.locator('.pet-mini-scroll')).toBeVisible();
      await page.locator('.pet-mini-swap').evaluate(async (node) => {
        await Promise.all(node.getAnimations().map((animation) => animation.finished));
      });
      expect(await page.locator('.pet-mini-panel').evaluate((node) => getComputedStyle(node).backgroundColor)).toBe('rgba(0, 0, 0, 0)');
      // Consume the actual reporter's output at its transport boundary. The
      // native poller must receive the card's rectangle but no inter-card gap.
      await expect.poll(() => page.evaluate(() => {
        const [first, second] = Array.from(document.querySelectorAll('.pet-mini-card'), (node) => node.getBoundingClientRect());
        const covers = (x: number, y: number) => window.petLayoutHitRegions.some((rect) => x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height);
        const x = (first.left + first.right) / 2;
        return { card: covers(x, (first.top + first.bottom) / 2), gap: covers(x, (first.bottom + second.top) / 2) };
      })).toEqual({ card: true, gap: false });
      // A long nowrap title and long detail must remain inside the fixed list.
      const widths = await page.locator('.pet-mini-scroll').evaluate((scroll) => ({
        client: scroll.clientWidth, content: scroll.scrollWidth,
        cards: Array.from(scroll.querySelectorAll('.pet-mini-card'), (card) => card.getBoundingClientRect().width),
      }));
      expect(widths.content).toBe(widths.client);
      expect(widths.cards.every((width) => width <= widths.client)).toBe(true);
      if (main) await page.locator('.pet-mini-reply-toggle').click();
      expect(await page.locator('.pet-mini-avatar-button').boundingBox()).toEqual(avatar);
      expect(errors).toEqual([]);
    });
  }
}

for (const cards of [2, 4, 20]) {
  test(`caps collapsed layers for ${cards} cards`, async ({ page }) => {
    await page.goto(`/e2e/pet-mini/fixture.html?activities=${cards}&layers=1`);
    await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
    await expect(page.locator('.pet-mini-layer')).toHaveCount(Math.min(cards - 1, 3));
    await expect(page.locator('.pet-mini-collapsed')).toHaveAttribute(
      'aria-label',
      `Expand ${cards} session cards`,
    );
  });
}

// One card is never a stack; see "one card is always drawn in full".
for (const cards of [2, 12, 120]) {
  test(`shows the collapsed count for ${cards} cards`, async ({ page }) => {
    await page.goto(`/e2e/pet-mini/fixture.html?activities=${cards}&layers=1`);
    await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
    const displayCount = cards > 99 ? '99+' : String(cards);
    await expect(page.locator('.pet-mini-stack-count')).toHaveText(`${displayCount} items`);
    await expect(page.locator('.pet-mini-collapsed')).toHaveAttribute(
      'aria-label',
      `Expand ${displayCount} session cards`,
    );
  });
}

for (const theme of ['light', 'dark']) {
  for (const cards of [2, 4, 20]) {
    test(`captures collapsed stack count (${theme}, ${cards} cards)`, async ({ page }, testInfo) => {
      await page.goto(`/e2e/pet-mini/fixture.html?activities=${cards}&layers=1`);
      await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
      await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
      await page.locator('.pet-mini-swap').evaluate(async (node) => {
        await Promise.all(node.getAnimations().map((animation) => animation.finished));
      });
      await page.screenshot({ path: testInfo.outputPath(`collapsed-${theme}-${cards}.png`), scale: 'css', animations: 'disabled' });
    });
  }
}

test('no mini surface casts a shadow the transparent window would clip', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?main=true&layers=2&sessions=4&queued=1');
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  // Every mini rule, rendered or not (input bubble, marquee, setup card): the
  // window clips a shadow to a visible box while content moves.
  const ruleShadows = await page.evaluate(() => {
    const found: string[] = [];
    const visit = (rules: CSSRuleList) => {
      for (const rule of Array.from(rules)) {
        if (rule instanceof CSSStyleRule && rule.selectorText.includes('pet-mini')) {
          const value = rule.style.boxShadow || rule.style.filter;
          if (value && value !== 'none') found.push(`${rule.selectorText}: ${value}`);
        }
        if ('cssRules' in rule) visit((rule as CSSGroupingRule).cssRules);
      }
    };
    for (const sheet of Array.from(document.styleSheets)) visit(sheet.cssRules);
    return found;
  });
  expect(ruleShadows).toEqual([]);
  const shadowed = () => page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>('.pet-mini-stage *'))
    .filter((node) => getComputedStyle(node).boxShadow !== 'none')
    .map((node) => node.className.toString()));
  expect(await shadowed()).toEqual([]);
  await page.locator('.pet-mini-collapsed').click();
  await page.locator('.pet-mini-reply-toggle').click();
  await expect(page.getByRole('button', { name: 'Switch session' })).toHaveCount(0);
  expect(await shadowed()).toEqual([]);
});

test('Vibey and its dock are reported as the hover zone, and a shell hover shows the dock', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?main=true&layers=0');
  const dock = page.locator('.pet-mini-dock');
  await expect(page.locator('.pet-mini-avatar-button')).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const avatar = document.querySelector('.pet-mini-avatar-button')!.getBoundingClientRect();
    const x = avatar.left + avatar.width / 2, y = avatar.top + avatar.height / 2;
    return window.petLayoutHitRegions.filter((rect) => rect.hover).some((rect) =>
      x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height);
  })).toBe(true);
  // Cards take the mouse but are not part of the hover zone.
  await expect.poll(() => page.evaluate(() => {
    const card = document.querySelector('.pet-mini-card')!.getBoundingClientRect();
    const x = card.left + card.width / 2, y = card.top + card.height / 2;
    return window.petLayoutHitRegions.some((rect) => rect.hover && x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height);
  })).toBe(false);
  // The macOS shell reports hover itself: an unfocused window's webview gets none.
  await page.mouse.move(0, 0);
  await expect(dock).toHaveCSS('opacity', '0');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('avibe:pet-hover', { detail: { hover: true } })));
  await expect(dock).toHaveCSS('opacity', '1');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('avibe:pet-hover', { detail: { hover: false } })));
  await expect(dock).toHaveCSS('opacity', '0');
});

for (const theme of ['light', 'dark']) {
  test(`dock active states use the hover surface (${theme})`, async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?main=true&layers=0');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const dock = page.locator('.pet-mini-dock');
    await page.locator('.pet-mini-avatar-button').hover();
    await expect(dock).toHaveCSS('opacity', '1');
    const keyboard = dock.locator('button').last();
    await keyboard.click();
    await expect(keyboard).toHaveClass(/is-typing/);

    const typingSurface = await keyboard.evaluate((node) => getComputedStyle(node).backgroundColor);
    const typingColor = await keyboard.evaluate((node) => getComputedStyle(node).color);
    expect(typingSurface).not.toBe('rgb(255, 255, 255)');

    await keyboard.evaluate((node) => node.classList.remove('is-typing'));
    await keyboard.hover();
    const hoverSurface = await keyboard.evaluate((node) => getComputedStyle(node).backgroundColor);
    expect(typingSurface).toBe(hoverSurface);

    await keyboard.evaluate((node) => node.classList.add('is-listening'));
    const listeningSurface = await keyboard.evaluate((node) => getComputedStyle(node).backgroundColor);
    const listeningColor = await keyboard.evaluate((node) => getComputedStyle(node).color);
    const regularColor = await keyboard.evaluate((node) => {
      node.classList.remove('is-listening');
      return getComputedStyle(node).color;
    });
    expect(listeningSurface).toBe(hoverSurface);
    expect(typingColor).toBe(regularColor);
    expect(listeningColor).toBe(regularColor);
  });
}

test('the reply bubble names Vibey as written', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?main=true&layers=0');
  const eyebrow = page.locator('.pet-mini-bubble-speaker');
  await expect(eyebrow).toBeVisible();
  // innerText applies text-transform, so this is what the user reads.
  expect(await eyebrow.evaluate((node) => node.innerText)).toContain('Vibey');
});

test('expanded dismiss controls stay local to hover and focus', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&activities=2');
  await page.locator('.pet-mini-collapsed').click();
  await page.locator('.pet-mini-swap').evaluate(async (node) => {
    await Promise.all(node.getAnimations().map((animation) => animation.finished));
  });
  await page.mouse.move(0, 0);
  const cards = page.locator('.pet-mini-card');
  const dismisses = page.locator('.pet-mini-dismiss');
  await expect(cards).toHaveCount(2);
  await expect(dismisses).toHaveCount(2);
  const before = await cards.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().toJSON()));
  for (const dismiss of await dismisses.all()) {
    await expect(dismiss).toHaveCSS('opacity', '0');
    await expect(dismiss).toHaveCSS('pointer-events', 'none');
  }

  await cards.nth(0).hover();
  expect(await cards.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().toJSON()))).toEqual(before);
  await expect(dismisses.nth(0)).toHaveCSS('opacity', '1');
  await expect(dismisses.nth(0)).toHaveCSS('pointer-events', 'auto');
  await expect(dismisses.nth(1)).toHaveCSS('opacity', '0');
  await expect(dismisses.nth(1)).toHaveCSS('pointer-events', 'none');

  await page.mouse.move(0, 0);
  await cards.nth(1).locator('.pet-mini-card-reference').focus();
  await expect(dismisses.nth(0)).toHaveCSS('opacity', '0');
  await expect(dismisses.nth(1)).toHaveCSS('opacity', '1');
  await expect(dismisses.nth(1)).toHaveCSS('pointer-events', 'auto');
});

test.describe('expanded dismiss controls on touch', () => {
  test.use({ hasTouch: true });

  test('remain visible when hover is unavailable', async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&activities=2');
    await page.locator('.pet-mini-collapsed').click();
    await expect(page.locator('.pet-mini-card')).toHaveCount(2);
    expect(await page.evaluate(() => matchMedia('(hover: none)').matches)).toBe(true);
    for (const dismiss of await page.locator('.pet-mini-dismiss').all()) {
      await expect(dismiss).toHaveCSS('opacity', '1');
      await expect(dismiss).toHaveCSS('pointer-events', 'auto');
    }
  });
});

for (const theme of ['light', 'dark']) {
  test(`captures expanded dismiss hover state (${theme})`, async ({ page }, testInfo) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&activities=2');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    await page.locator('.pet-mini-collapsed').click();
    await expect(page.locator('.pet-mini-card')).toHaveCount(2);
    await page.locator('.pet-mini-card').first().hover();
    await page.screenshot({ path: testInfo.outputPath(`expanded-dismiss-${theme}.png`), scale: 'css', animations: 'disabled' });
  });
}


test('attention leads the list and referencing sends only through the main composer', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal');
  await page.locator('.pet-mini-collapsed').click();
  const first = page.locator('.pet-mini-card').first();
  await expect(first).toHaveAttribute('data-session-id', 'ses_fixture_0');
  await expect(first).toHaveClass(/is-needs-input/);
  await first.getByRole('button', { name: 'Open in Avibe', exact: true }).click();
  await expect(page.getByRole('textbox')).toHaveCount(0);
  await first.locator('.pet-mini-card-reference').click();
  await expect(page.locator('.pet-mini-reference-chip')).toHaveCount(1);
  await expect(page.locator('.pet-mini-reply-bubble')).toHaveCount(0);
  await page.getByRole('textbox').fill('请总结进展');
  await page.getByRole('textbox').press('Enter');
  await expect.poll(() => page.evaluate(() => window.petFixtureSends)).toEqual([{
    sessionId: 'ses_main', payload: { text: '#<ses_fixture_0> 请总结进展', content: { references: [{ kind: 'session', session_id: 'ses_fixture_0', title: '修复桌宠语音输入' }] } },
  }]);
  await expect(page.locator('.pet-mini-reply-bubble')).toBeVisible();
});

test('reply history and input remain within the fixed window with reduced motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&queued=1');
  await page.locator('.pet-mini-collapsed').click();
  const avatar = await page.locator('.pet-mini-avatar-button').boundingBox();
  await page.locator('.pet-mini-reply-toggle').click();
  await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
  const history = await page.locator('.pet-mini-bubble-history').boundingBox();
  expect(history!.y).toBeGreaterThanOrEqual(0);
  expect(history!.y + history!.height).toBeLessThanOrEqual(600);
  expect(await page.locator('.pet-mini-avatar-button').boundingBox()).toEqual(avatar);
  expect(await page.locator('.pet-mini-detail').evaluate((node) => parseFloat(getComputedStyle(node).transitionDuration))).toBeLessThan(0.01);
});

for (const theme of ['light', 'dark']) {
  for (const reduced of [false, true]) {
    test(`history and cards share expansion with reachable actions (${theme}, reduced=${reduced})`, async ({ page }) => {
      await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
      await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=actions&queued=1');
      await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
      const avatar = page.locator('.pet-mini-avatar-button');
      await expect(avatar).toBeVisible();
      const anchor = await avatar.boundingBox();
      await page.locator('.pet-mini-collapsed').click();
      await expect(page.locator('.pet-mini-scroll')).toBeVisible();
      await page.locator('.pet-mini-reply-toggle').click();
      await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
      await expect(page.locator('.pet-mini-scroll')).toHaveCount(0);
      await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
      await page.evaluate(async () => {
        await Promise.all(document.getAnimations()
          .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
          .map((animation) => animation.finished.catch(() => {})));
      });
      const history = page.locator('.pet-mini-bubble-history');
      // Open in Avibe is pinned outside the scroll box; see the history test below.
      for (const name of ['需要授权，在 Avibe 中打开', '继续']) {
        const button = history.getByRole('button', { name, exact: true });
        await expect(button).toBeVisible();
        const bounds = await button.boundingBox(), box = await history.boundingBox();
        expect(bounds!.y).toBeGreaterThanOrEqual(box!.y);
        expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(box!.y + box!.height + 1);
      }
      expect(await history.evaluate((node) => node.scrollHeight <= node.clientHeight + 1)).toBe(true);
      expect(await history.evaluate((node) => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop))).toBeLessThanOrEqual(1);
      expect((await page.locator('.pet-mini-collapsed').boundingBox())!.y).toBeGreaterThanOrEqual(0);
      expect(await avatar.boundingBox()).toEqual(anchor);
      await page.locator('.pet-mini-collapsed').click();
      await expect(page.locator('.pet-mini-scroll')).toBeVisible();
      await expect(page.locator('.pet-mini-detail')).not.toHaveClass(/is-open/);
      await page.locator('.pet-mini-reply-toggle').click();
      await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
      await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
      // Vibey closes the open history, then opens it again with the stack
      // still collapsed; the stack expands the list and closes the history;
      // Vibey then collapses the list.
      await avatar.click();
      await expect(page.locator('.pet-mini-detail')).not.toHaveClass(/is-open/);
      await avatar.click();
      await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
      await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
      await page.locator('.pet-mini-collapsed').click();
      await expect(page.locator('.pet-mini-scroll')).toBeVisible();
      await expect(page.locator('.pet-mini-detail')).not.toHaveClass(/is-open/);
      await avatar.click();
      await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
      await expect(page.locator('.pet-mini-detail')).not.toHaveClass(/is-open/);
      expect(await avatar.boundingBox()).toEqual(anchor);
    });
  }
}


test('an empty activity stack takes no space while history remains usable without a switcher', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&activities=0&queued=1');
  await page.locator('.pet-mini-reply-toggle').waitFor();
  const avatar = await page.locator('.pet-mini-avatar-button').boundingBox();
  // With no cards, a Vibey click opens the history straight away.
  await page.locator('.pet-mini-avatar-button').click();
  await expect(page.locator('.pet-mini-cards')).toHaveCSS('height', '0px');
  await expect(page.locator('.pet-mini-panel')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Switch session' })).toHaveCount(0);
  await expect(page.locator('.pet-mini-session-option')).toHaveCount(0);
  await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
  await expect(page.locator('.pet-mini-cards')).toHaveCSS('height', '0px');
  expect(await page.locator('.pet-mini-avatar-button').boundingBox()).toEqual(avatar);
});

test('a close during a main outage lasts until the mini document reopens', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal');
  await page.locator('.pet-mini-collapsed').click();
  const card = page.locator('.pet-mini-card[data-session-id="ses_fixture_0"]');
  await expect(card).toBeVisible();
  await page.evaluate(() => {
    window.petFixtureConnection(false);
    window.setPetMainAvailable(false);
    window.petFixtureConnection(true);
  });
  await expect(page.getByText("Couldn't open the conversation with Vibey. Try again in a moment.")).toBeVisible();
  await card.hover();
  await card.locator('.pet-mini-dismiss').click();
  await expect(card).toHaveCount(0);
  await page.evaluate(() => {
    window.petFixtureConnection(false);
    window.setPetMainAvailable(true);
    window.petFixtureConnection(true);
  });
  await expect(page.locator('.pet-mini-reply-toggle')).toBeVisible();
  await expect(card).toHaveCount(0);
  await page.reload();
  await page.locator('.pet-mini-collapsed').click();
  await expect(card).toBeVisible();
});

for (const trigger of ['reconnect', 'reactivation']) {
  test(`main resolves automatically after service recovery on ${trigger}`, async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?offline=true&activities=0');
    await expect(page.getByText("Couldn't open the conversation with Vibey. Try again in a moment.")).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open in Avibe', exact: true })).toBeVisible();
    await expect(page.getByRole('textbox')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Switch session' })).toHaveCount(0);
    if (trigger === 'reconnect') {
      await page.evaluate(() => {
        window.petFixtureConnection(false);
        window.setPetMainAvailable(true);
        window.petFixtureConnection(true);
      });
    } else {
      await page.evaluate(() => {
        Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => false });
        window.dispatchEvent(new Event('blur'));
        window.setPetMainAvailable(true);
        Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => true });
        window.dispatchEvent(new Event('focus'));
      });
    }
    await expect(page.locator('.pet-mini-reply-bubble')).toContainText('Ready to help');
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('avibe:pet-summon', { detail: { intent: 'listen' } })));
    await page.getByRole('textbox').fill('recovered');
    await page.getByRole('textbox').press('Enter');
    await expect.poll(() => page.evaluate(() => window.petFixtureSends)).toEqual([{ sessionId: 'ses_main', payload: { text: 'recovered' } }]);
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('avibe.pet.devBinding')!).binding)).toBe('ses_main');
  });
}

for (const pose of ['idle', 'ready', 'sleeping']) {
  test(`layered art animates while visible but unfocused and pauses only when hidden (${pose})`, async ({ page }) => {
    await page.goto(`/e2e/pet-mini/fixture.html?pose=${pose}`);
    const art = page.locator('.vibey-art');
    await expect(art).toBeVisible();
    // The loops live on the layers; the root's one-shot pose-change squash is
    // allowed to finish, so it is not part of the reading.
    const playStates = () => art.evaluate((node) => [...node.querySelectorAll('*')]
      .map((element) => getComputedStyle(element))
      .filter((style) => style.animationName !== 'none')
      .map((style) => style.animationPlayState));

    // The pet window is shown without ever taking focus.
    await page.evaluate(() => {
      Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => false });
      window.dispatchEvent(new Event('blur'));
    });
    await expect(art).toHaveAttribute('data-active', 'true');
    const unfocused = await playStates();
    expect(unfocused.length).toBeGreaterThan(0);
    expect(new Set(unfocused)).toEqual(new Set(['running']));

    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await expect(art).toHaveAttribute('data-active', 'false');
    expect(new Set(await playStates())).toEqual(new Set(['paused']));

    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await expect(art).toHaveAttribute('data-active', 'true');
    expect(new Set(await playStates())).toEqual(new Set(['running']));
  });
}

test('the reply bubble tail starts on the bubble border instead of poking above it', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal');
  const bubble = page.locator('.pet-mini-reply-bubble');
  await expect(bubble).toBeVisible();
  const tail = await bubble.evaluate((node) => {
    const style = getComputedStyle(node, '::after');
    return { bottom: parseFloat(style.bottom), height: parseFloat(style.height), clip: style.clipPath, transform: style.transform };
  });
  // Rotated 45deg, the square's anti-diagonal is the horizontal line through
  // its centre, where both strokes end. That centre must sit on the top edge of
  // the bubble's bottom border (the padding edge `bottom` is measured from),
  // and only the half below it may paint.
  expect(tail.transform).not.toBe('none');
  expect(tail.bottom).toBe(-tail.height / 2);
  expect(tail.clip).toBe('polygon(100% 0px, 100% 100%, 0px 100%)');
});

for (const theme of ['light', 'dark']) {
  test(`history opens above a toggle that keeps its place (${theme})`, async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&queued=1');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const settle = () => page.evaluate(async () => {
      await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => {})));
    });
    const toggle = page.locator('.pet-mini-reply-toggle');
    const detail = page.locator('.pet-mini-reply-bubble .pet-mini-detail');
    const history = page.locator('.pet-mini-bubble-history');
    const near = (a: { x: number; y: number; width: number; height: number } | null, b: typeof a) => {
      for (const key of ['x', 'y', 'width', 'height'] as const) expect(Math.abs(a![key] - b![key])).toBeLessThanOrEqual(1);
    };
    await expect(toggle).toBeVisible();
    await settle();
    const collapsed = await toggle.boundingBox();
    expect(await toggle.getAttribute('aria-controls')).toBe(await detail.getAttribute('id'));
    await expect(toggle.locator('svg')).toHaveClass(/lucide-chevron-up/);
    await expect(toggle.locator('svg')).toHaveCSS('transform', 'none');
    expect(await page.evaluate(() => window.petFixtureMarkReads)).toEqual([]);

    await toggle.click();
    await expect(detail).toHaveClass(/is-open/);
    await settle();
    near(await toggle.boundingBox(), collapsed);
    // Opening upward: the history sits entirely above the toggle row.
    const box = (await history.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(collapsed!.y + 1);
    expect(box.y).toBeGreaterThanOrEqual(0);
    // The newest result is shown on open, so the earlier turns sit scrolled
    // away under the top edge fade.
    // Chat-style: it opens at the very bottom, so there is no bottom fade and
    // the newest result is in view right above the toggle row.
    expect(await history.evaluate((node) => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop))).toBeLessThanOrEqual(1);
    expect(await history.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
    const last = (await history.locator('.pet-mini-detail-result').last().boundingBox())!;
    expect(last.y + last.height).toBeGreaterThan(box.y);
    expect(last.y).toBeLessThan(box.y + box.height);
    expect(await history.evaluate((node) => /#000 [1-9]\d*px|rgb\(0, 0, 0\) [1-9]\d*px/.test(node.style.maskImage || node.style.webkitMaskImage))).toBe(true);
    await expect(toggle.locator('svg')).not.toHaveCSS('transform', 'none');
    await expect.poll(() => page.evaluate(() => window.petFixtureMarkReads)).toEqual(['ses_main']);

    await toggle.click();
    await expect(detail).not.toHaveClass(/is-open/);
    await settle();
    near(await toggle.boundingBox(), collapsed);
  });
}

for (const theme of ['light', 'dark']) {
  test(`Open in Avibe is an icon pinned to the open history's corner (${theme})`, async ({ page, browserName }) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&queued=1');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const settle = () => page.evaluate(async () => {
      await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => {})));
    });
    const toggle = page.locator('.pet-mini-reply-toggle');
    const history = page.locator('.pet-mini-bubble-history');
    const icon = page.locator('.pet-mini-reply-bubble').getByRole('button', { name: '在 Avibe 中打开', exact: true });
    await expect(toggle).toBeVisible();
    await expect(icon).toHaveCount(0);

    await toggle.click();
    await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
    await settle();
    await expect(icon).toBeVisible();
    expect(await icon.getAttribute('title')).toBe('');
    expect((await icon.innerText()).trim()).toBe('');
    // Only the pinned icon remains: no text link inside the scrolling history.
    await expect(history.getByRole('button', { name: '在 Avibe 中打开', exact: true })).toHaveCount(0);
    expect(await history.evaluate((node) => getComputedStyle(node).scrollbarWidth)).toBe('none');

    const pinned = (await icon.boundingBox())!;
    expect(pinned.width).toBeGreaterThanOrEqual(28);
    expect(pinned.height).toBeGreaterThanOrEqual(28);
    // Inset equally from the bubble's visible edge on the top and the right.
    const bubble = page.locator('.pet-mini-reply-bubble');
    const insets = async () => {
      const [outer, button] = [(await bubble.boundingBox())!, (await icon.boundingBox())!];
      return { top: button.y - outer.y, right: outer.x + outer.width - (button.x + button.width) };
    };

    // It stays put while the history scrolls, and at the top of the scroll it
    // covers none of the content.
    for (const top of ['top', 'middle', 'bottom'] as const) {
      await history.evaluate((node, top) => {
        node.scrollTop = top === 'top' ? 0 : top === 'bottom' ? node.scrollHeight : (node.scrollHeight - node.clientHeight) / 2;
      }, top);
      await expect.poll(() => icon.boundingBox()).toEqual(pinned);
      const { top: above, right } = await insets();
      expect(Math.abs(above - right)).toBeLessThanOrEqual(0.5);
      expect(above).toBeGreaterThanOrEqual(6);
    }
    await history.evaluate((node) => { node.scrollTop = 0; });
    const first = (await history.locator('.pet-mini-detail-inner > *').first().boundingBox())!;
    expect(first.y).toBeGreaterThanOrEqual(pinned.y + pinned.height);

    // Reachable with Tab from the toggle, with a visible focus ring.
    await toggle.focus();
    // WebKit on macOS only tabs to buttons with Option held (Full Keyboard Access).
    await page.keyboard.press(browserName === 'webkit' ? 'Alt+Tab' : 'Tab');
    await expect(icon).toBeFocused();
    await expect(icon).toHaveCSS('outline-style', 'solid');

    await icon.click();
    await expect.poll(() => page.evaluate(() => window.petFixtureOpens)).toEqual([expect.stringContaining('ses_main')]);

    await toggle.click();
    await expect(icon).toHaveCount(0);
  });
}

test('the history fades over a taller, darker band than the card list', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&activities=8&queued=1');
  const mask = (selector: string) => page.locator(selector).evaluate((node) => (node as HTMLElement).style.maskImage || (node as HTMLElement).style.webkitMaskImage);
  const scrollTo = (selector: string, where: 'top' | 'middle') => page.locator(selector).evaluate((node, where) => {
    node.scrollTop = where === 'top' ? 0 : (node.scrollHeight - node.clientHeight) / 2;
    node.dispatchEvent(new Event('scroll'));
    return node.scrollHeight - node.clientHeight;
  }, where);

  await page.locator('.pet-mini-collapsed').click();
  expect(await scrollTo('.pet-mini-scroll', 'middle')).toBeGreaterThan(64);
  await expect.poll(() => mask('.pet-mini-scroll')).toContain('rgb(0, 0, 0) 32px');
  expect(await mask('.pet-mini-scroll')).toContain('calc(100% - 32px)');
  expect(await mask('.pet-mini-scroll')).not.toContain('rgba');

  await page.locator('.pet-mini-reply-toggle').click();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  expect(await scrollTo('.pet-mini-bubble-history', 'middle')).toBeGreaterThan(96);
  // A 48px band that is already well faded across its outer two thirds.
  await expect.poll(() => mask('.pet-mini-bubble-history')).toContain('rgb(0, 0, 0) 48px');
  const middle = await mask('.pet-mini-bubble-history');
  for (const stop of ['rgba(0, 0, 0, 0.12) 16.8px', 'rgba(0, 0, 0, 0.55) 33.6px', 'calc(100% - 48px)', 'rgba(0, 0, 0, 0.55) calc(100% - 33.6px)', 'rgba(0, 0, 0, 0.12) calc(100% - 16.8px)']) {
    expect(middle).toContain(stop);
  }
  // No top fade before the history is scrolled.
  await scrollTo('.pet-mini-bubble-history', 'top');
  await expect.poll(() => mask('.pet-mini-bubble-history')).toContain('rgb(0, 0, 0) 0px');
});

test('the history opens at the bottom and stays pinned there only while the reader is', async ({ page }) => {
  // Everything is read, so each live reply replaces the shown one and nothing
  // here is about mark-read; the open-once mark-read is covered above.
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&unread=0&queued=1');
  const history = page.locator('.pet-mini-bubble-history');
  const gap = () => history.evaluate((node) => node.scrollHeight - node.clientHeight - node.scrollTop);
  const height = () => history.evaluate((node) => node.scrollHeight);
  await page.locator('.pet-mini-reply-toggle').click();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  await expect.poll(gap).toBeLessThanOrEqual(1);

  // Content arriving while the reader is at the bottom keeps them there.
  const before = await height();
  await page.evaluate(() => window.petFixtureMessage({ id: 'live_1', author: 'agent', type: 'result', text: '新的进展。'.repeat(30) }));
  await expect.poll(height).toBeGreaterThan(before);
  await expect.poll(gap).toBeLessThanOrEqual(1);

  // A reader who has scrolled up is left where they are.
  await history.evaluate((node) => { node.scrollTop = 0; });
  await expect.poll(() => history.evaluate((node) => node.scrollTop)).toBe(0);
  const grown = await height();
  await page.evaluate(() => window.petFixtureMessage({ id: 'live_2', author: 'agent', type: 'result', text: '又一条进展。'.repeat(60) }));
  await expect.poll(height).toBeGreaterThan(grown);
  expect(await history.evaluate((node) => node.scrollTop)).toBe(0);
  expect(await page.evaluate(() => window.petFixtureMarkReads)).toEqual([]);

  // A new latest input is a new place to read from: it re-opens at the end
  // even for a reader who had scrolled up.
  await page.evaluate(() => window.petFixtureMessage({ id: 'live_input', author: 'user', type: 'user', text: '再看看文档' }));
  await expect(history).toContainText('再看看文档');
  await expect.poll(gap).toBeLessThanOrEqual(1);
});

test('a latest reply taller than the history opens at its end', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&reply=long&queued=1');
  const history = page.locator('.pet-mini-bubble-history');
  await page.locator('.pet-mini-reply-toggle').click();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  expect(await history.evaluate((node) => node.scrollHeight - node.clientHeight)).toBeGreaterThan(100);
  await expect.poll(() => history.evaluate((node) => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop))).toBeLessThanOrEqual(1);
  const last = (await history.locator('.pet-mini-detail-result').last().boundingBox())!;
  const box = (await history.boundingBox())!;
  expect(last.y + last.height).toBeGreaterThan(box.y);
});

for (const theme of ['light', 'dark']) {
  test(`the open buttons show a pet tooltip on hover and keyboard focus (${theme})`, async ({ page, browserName }) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&queued=1');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const tooltip = page.getByRole('tooltip');
    // Fully on screen and on top: nothing (clip, mask, scroll box) covers it.
    const unclipped = async () => {
      const box = (await tooltip.boundingBox())!;
      const viewport = page.viewportSize()!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
      expect(await tooltip.evaluate((node) => {
        const rect = node.getBoundingClientRect();
        // The tooltip ignores the pointer; let hit testing see it for the check.
        (node as HTMLElement).style.pointerEvents = 'auto';
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        (node as HTMLElement).style.pointerEvents = '';
        return hit === node;
      })).toBe(true);
      return box;
    };

    // History icon: below it, right-aligned.
    await page.locator('.pet-mini-reply-toggle').click();
    const icon = page.locator('.pet-mini-history-open');
    await expect(icon).toBeVisible();
    // No native tooltip applies: the nearest title on its ancestor chain, which
    // the browser would show, is its own empty one.
    expect(await icon.evaluate((node) => node.closest('[title]')?.getAttribute('title'))).toBe('');
    await expect(tooltip).toHaveCount(0);
    await icon.hover();
    await expect(tooltip).toHaveText('在 Avibe 中打开');
    await expect(icon).toHaveAttribute('aria-describedby', (await tooltip.getAttribute('id'))!);
    const below = await unclipped();
    const button = (await icon.boundingBox())!;
    expect(below.y).toBeGreaterThanOrEqual(button.y + button.height);
    expect(Math.abs(below.x + below.width - (button.x + button.width))).toBeLessThanOrEqual(1);
    await page.mouse.move(2, 2);
    await expect(tooltip).toHaveCount(0);
    await icon.hover();
    await expect(tooltip).toBeVisible();
    // Escape takes the tooltip first and leaves the history open.
    await page.keyboard.press('Escape');
    await expect(tooltip).toHaveCount(0);
    await expect(icon).toBeVisible();

    // Keyboard focus shows it at once, without the hover delay.
    await page.mouse.move(2, 2);
    await page.locator('.pet-mini-reply-toggle').focus();
    await page.keyboard.press(browserName === 'webkit' ? 'Alt+Tab' : 'Tab');
    await expect(icon).toBeFocused();
    await expect(tooltip).toBeVisible({ timeout: 150 });
    await page.keyboard.press('Escape');
    await expect(tooltip).toHaveCount(0);

    // Card open button: to its left, so the card scroll box cannot clip it.
    await page.locator('.pet-mini-collapsed').click();
    const cardOpen = page.locator('.pet-mini-card .pet-mini-open').first();
    await expect(cardOpen).toBeVisible();
    // The card carries its title, which a descendant inherits unless it sets
    // its own: an empty one suppresses that native tooltip.
    expect(await cardOpen.evaluate((node) => node.parentElement?.closest('[title]:not([title=""])') != null)).toBe(true);
    expect(await cardOpen.getAttribute('title')).toBe('');
    await cardOpen.hover();
    await expect(tooltip).toHaveText('在 Avibe 中打开');
    const left = await unclipped();
    const card = (await cardOpen.boundingBox())!;
    expect(left.x + left.width).toBeLessThanOrEqual(card.x);
    await page.mouse.move(2, 2);
    await expect(tooltip).toHaveCount(0);
  });
}

for (const theme of ['light', 'dark']) {
  test(`one card is always drawn in full and a Vibey click does not change it (${theme})`, async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?activities=1&queued=1');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const settle = () => page.evaluate(async () => {
      await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => {})));
    });
    const card = page.locator('.pet-mini-card');
    const stop = card.locator('.pet-mini-stop');
    const open = card.locator('.pet-mini-open');
    const avatar = page.locator('.pet-mini-avatar-button');
    await expect(card).toHaveCount(1);
    await expect(stop).toBeVisible();
    await expect(open).toBeVisible();
    await expect(page.locator('.pet-mini-collapsed')).toHaveCount(0);
    await expect(page.locator('.pet-mini-stack-count')).toHaveCount(0);
    await expect(avatar).toHaveAttribute('aria-expanded', 'false');
    // The fixture's reply lands just after load and opens the bubble below.
    await expect(page.locator('.pet-mini-slot.is-open .pet-mini-reply-bubble')).toBeVisible();
    await settle();
    const size = async () => { const box = (await card.boundingBox())!; return { width: box.width, height: box.height }; };
    const before = await size();

    // A Vibey click opens and closes the history; the card moves with the
    // bubble beneath it but keeps its full form.
    for (const historyOpen of [true, false]) {
      await avatar.click();
      await settle();
      expect(await size()).toEqual(before);
      await expect(stop).toBeVisible();
      await expect(open).toBeVisible();
      await expect(page.locator('.pet-mini-collapsed')).toHaveCount(0);
      await expect(avatar).toHaveAttribute('aria-expanded', String(historyOpen));
    }

    // The window passes clicks through except over hit regions, so every
    // control of the card must be one, without opening anything first.
    const inRegions = async (locator: typeof card) => {
      const target = (await locator.boundingBox())!;
      const x = target.x + target.width / 2, y = target.y + target.height / 2;
      return page.evaluate(([x, y]) => window.petLayoutHitRegions.some((rect) =>
        x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height), [x, y]);
    };
    await expect.poll(() => inRegions(stop)).toBe(true);
    await expect.poll(() => inRegions(open)).toBe(true);
    await card.hover();
    const dismiss = page.locator('.pet-mini-dismiss');
    await expect(dismiss).toBeVisible();
    await expect.poll(() => inRegions(dismiss)).toBe(true);

    // The lone card's open button is the shared one: the pet tooltip, and no
    // native tooltip inherited from the card's title.
    expect(await open.getAttribute('title')).toBe('');
    await open.hover();
    await expect(page.getByRole('tooltip')).toHaveText('Open in Avibe');
    await open.click();
    await expect.poll(() => page.evaluate(() => window.petFixtureOpens)).toEqual(['avibe://session/ses_fixture_0']);
    await stop.click();
    await expect(stop).toHaveAttribute('aria-label', 'Stop?');
    await dismiss.click();
    await expect(card).toHaveCount(0);
  });

  test(`one card stays in full beside the open history and everything fits (${theme})`, async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&activities=1&history=long&queued=1');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const card = page.locator('.pet-mini-card');
    await expect(card.locator('.pet-mini-open')).toBeVisible();
    const anchor = await page.locator('.pet-mini-avatar-button').boundingBox();
    await page.locator('.pet-mini-reply-toggle').click();
    await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
    await page.evaluate(async () => {
      await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => {})));
    });
    await expect(card).toBeVisible();
    await expect(card.locator('.pet-mini-open')).toBeVisible();
    const cardBox = (await card.boundingBox())!;
    const bubble = (await page.locator('.pet-mini-reply-bubble').boundingBox())!;
    const avatar = (await page.locator('.pet-mini-avatar-button').boundingBox())!;
    expect(cardBox.y).toBeGreaterThanOrEqual(0);
    expect(cardBox.y + cardBox.height).toBeLessThanOrEqual(bubble.y);
    expect(avatar).toEqual(anchor);
    expect(bubble.y + bubble.height).toBeLessThanOrEqual(600);
    expect(avatar.y + avatar.height).toBeLessThanOrEqual(600);
  });
}

test('Vibey clicks with a lone card leave no hidden expansion for a second card', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?activities=1');
  const avatar = page.locator('.pet-mini-avatar-button');
  await expect(page.locator('.pet-mini-card')).toHaveCount(1);
  // Open, then close, the history: nothing is left expanded behind it.
  await avatar.click();
  await expect(avatar).toHaveAttribute('aria-expanded', 'true');
  await avatar.click();
  await expect(avatar).toHaveAttribute('aria-expanded', 'false');
  await page.evaluate(() => window.petFixtureAddActivity());
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  await expect(page.locator('.pet-mini-stack-count')).toHaveText('2 items');
  await expect(page.locator('.pet-mini-scroll')).toHaveCount(0);
  await expect(avatar).toHaveAttribute('aria-expanded', 'false');
});

test('with a lone card, Vibey closes the open history and leaves the card alone', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&activities=1&queued=1');
  const card = page.locator('.pet-mini-card');
  const avatar = page.locator('.pet-mini-avatar-button');
  await expect(card.locator('.pet-mini-open')).toBeVisible();
  await page.locator('.pet-mini-reply-toggle').click();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  const size = async () => {
    await page.evaluate(async () => {
      await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => {})));
    });
    const box = (await card.boundingBox())!;
    return { width: box.width, height: box.height };
  };
  const before = await size();
  await avatar.click();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).not.toHaveClass(/is-open/);
  // The card moves down with the shrinking bubble, but keeps its full form.
  await expect(card.locator('.pet-mini-open')).toBeVisible();
  await expect(card.locator('.pet-mini-authorize')).toBeVisible();
  expect(await size()).toEqual(before);
  // Nothing was left expanded behind it either.
  await page.evaluate(() => window.petFixtureAddActivity());
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  await expect(page.locator('.pet-mini-scroll')).toHaveCount(0);
});

test('two or more cards expand from the stack, and a Vibey click collapses them', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?activities=2');
  const avatar = page.locator('.pet-mini-avatar-button');
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  await expect(page.locator('.pet-mini-stack-count')).toBeVisible();
  // The stack is the way to expand the cards.
  await page.locator('.pet-mini-collapsed').click();
  await expect(page.locator('.pet-mini-scroll')).toBeVisible();
  await expect(page.locator('.pet-mini-card')).toHaveCount(2);
  await expect(avatar).toHaveAttribute('aria-expanded', 'false');
  // Vibey collapses the expanded list and does nothing else.
  await avatar.click();
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).not.toHaveClass(/is-open/);
  // The next click is about Vibey: it opens the history, and the stack stays collapsed.
  await avatar.click();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
  await expect(avatar).toHaveAttribute('aria-expanded', 'true');
  // Expanding the list closes the history: the two never share the space.
  await page.locator('.pet-mini-collapsed').click();
  await expect(page.locator('.pet-mini-scroll')).toBeVisible();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).not.toHaveClass(/is-open/);
  await expect(avatar).toHaveAttribute('aria-expanded', 'false');
});

// Vibey's bubble: shown only while the main session is active, hidden the
// moment it is not (spec §4.2).
const bubbleShown = (page: import('@playwright/test').Page) => page.locator('.pet-mini-slot.is-open .pet-mini-reply-bubble');
const bubbleSlot = (page: import('@playwright/test').Page) => page.locator('.pet-mini-slot:has(.pet-mini-reply-bubble)');
// Whether the bubble itself is reported as a region (a card moving into its old
// place is, rightly, a region of its own).
const bubbleIsRegion = (page: import('@playwright/test').Page) => page.evaluate(() => {
  const box = document.querySelector('.pet-mini-reply-bubble')!.getBoundingClientRect();
  return window.petLayoutHitRegions.some((rect) => Math.abs(rect.x - box.left) <= 1 && Math.abs(rect.width - box.width) <= 1 && rect.height > 1
    && Math.abs(rect.y + rect.height - box.bottom) <= 1);
});
const settleMotion = (page: import('@playwright/test').Page) => page.evaluate(async () => {
  await Promise.all(document.getAnimations()
    .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
    .map((animation) => animation.finished.catch(() => {})));
});

test('an old, finished exchange starts with the bubble hidden, and takes no space or clicks', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh');
  await expect(page.locator('.pet-mini-avatar-button')).toBeVisible();
  await expect(page.locator('.pet-mini-reply-bubble')).toHaveCount(1);
  await expect(bubbleShown(page)).toHaveCount(0);
  await settleMotion(page);
  expect(await bubbleSlot(page).evaluate((node) => node.getBoundingClientRect().height)).toBeLessThan(1);
  await expect.poll(() => bubbleIsRegion(page)).toBe(false);
});

// The reply lingers for 10s of on-screen time after the turn ends (spec §4.2).
// It waits until the running turn and its conversation are on screen, so the
// turn's end is one this page watched happen.
const finishTurn = async (page: import('@playwright/test').Page, text = '做完了。') => {
  await expect(page.locator('.pet-mini-reply-bubble')).toContainText('检查桌宠 mini 的交互');
  await page.evaluate((text) =>
    window.petFixtureFinishTurn({ id: 'live_reply', author: 'agent', type: 'result', text, read_at: null }), text);
};
// Tab (Option+Tab in WebKit, whose Tab skips buttons) until the toggle row has
// keyboard focus; with no cards it is the first stop.
const tabToToggle = async (page: import('@playwright/test').Page, browserName: string) => {
  const toggle = page.locator('.pet-mini-reply-toggle');
  for (let step = 0; step < 6 && !(await toggle.evaluate((node) => node === document.activeElement)); step += 1) {
    await page.keyboard.press(browserName === 'webkit' ? 'Alt+Tab' : 'Tab');
  }
  await expect(toggle).toBeFocused();
  expect(await toggle.evaluate((node) => node.matches(':focus-visible'))).toBe(true);
  return toggle;
};

for (const theme of ['light', 'dark']) {
  test(`a running turn shows thinking, then its reply for 10s on screen (${theme})`, async ({ page }) => {
    await page.clock.install();
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&running=1&asr=1');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    await page.clock.runFor(500);
    await expect(bubbleShown(page)).toBeVisible();
    await expect(page.locator('.pet-mini-reply-toggle')).toContainText('思考中');
    await finishTurn(page);
    await page.clock.runFor(100);
    await expect(bubbleShown(page)).toBeVisible();
    await expect(page.locator('.pet-mini-reply-toggle')).toContainText('已回复：做完了。');
    await page.clock.runFor(9_500);
    await expect(bubbleShown(page)).toBeVisible();
    await page.clock.runFor(1_000);
    await expect(bubbleShown(page)).toHaveCount(0);
    await settleMotion(page);
    await page.clock.runFor(100);
    await expect.poll(() => bubbleIsRegion(page)).toBe(false);
  });
}

test('a background completion arriving during the linger does not take over its summary', async ({ page }) => {
  await page.clock.install();
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&running=1');
  await page.clock.runFor(500);
  await finishTurn(page);
  await page.clock.runFor(1_000);
  await expect(page.locator('.pet-mini-reply-toggle')).toContainText('已回复：做完了。');
  await page.evaluate(() => window.petFixtureMessage({
    id: 'background', author: 'agent', type: 'result', text: '后台任务也完成了。', read_at: null, metadata: { detached: true },
  }));
  await page.clock.runFor(500);
  await expect(page.locator('.pet-mini-reply-toggle')).toContainText('已回复：做完了。');
  await expect(page.locator('.pet-mini-reply-toggle')).not.toContainText('后台任务');
});

test('the pointer over the lingering reply holds it, and leaving leaves at least 3s', async ({ page }) => {
  await page.clock.install();
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&running=1');
  await page.clock.runFor(500);
  await finishTurn(page);
  await page.clock.runFor(9_000);
  await page.locator('.pet-mini-reply-toggle').hover();
  await page.clock.runFor(60_000);
  await expect(bubbleShown(page)).toBeVisible();
  await page.mouse.move(2, 2);
  await page.clock.runFor(2_800);
  await expect(bubbleShown(page)).toBeVisible();
  await page.clock.runFor(400);
  await expect(bubbleShown(page)).toHaveCount(0);
});

test('keyboard focus in the lingering reply keeps it until focus moves on', async ({ page, browserName }) => {
  await page.clock.install();
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&running=1&activities=0');
  await page.clock.runFor(500);
  await finishTurn(page);
  await page.clock.runFor(100);
  await tabToToggle(page, browserName);
  await page.clock.runFor(30_000);
  await expect(bubbleShown(page)).toBeVisible();
  // Focus moves on: the next look finds nothing holding it, and it goes.
  await page.locator('.pet-mini-avatar-button').focus();
  await page.clock.runFor(1_500);
  await expect(bubbleShown(page)).toHaveCount(0);
});

test('a Vibey click on the lingering reply opens its history, which does not linger after', async ({ page }) => {
  await page.clock.install();
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&running=1');
  await page.clock.runFor(500);
  await finishTurn(page);
  await page.clock.runFor(2_000);
  const avatar = page.locator('.pet-mini-avatar-button');
  await avatar.click();
  await page.clock.runFor(300);
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  await expect.poll(() => page.locator('.pet-mini-bubble-history').evaluate((node) => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop))).toBeLessThanOrEqual(1);
  await avatar.click();
  await page.clock.runFor(300);
  await expect(bubbleShown(page)).toHaveCount(0);
});

test('the lingering reply\'s toggle row opens its history; closing it hides the bubble and hands focus to Vibey', async ({ page, browserName }) => {
  await page.clock.install();
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&running=1&activities=0');
  await page.clock.runFor(500);
  await finishTurn(page);
  await page.clock.runFor(1_000);
  // From the keyboard, so focus is inside the bubble as it goes (WebKit does
  // not focus a button on a mouse click).
  await tabToToggle(page, browserName);
  await page.keyboard.press('Enter');
  await page.clock.runFor(300);
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  await page.keyboard.press('Enter');
  await page.clock.runFor(300);
  await expect(bubbleShown(page)).toHaveCount(0);
  await expect(page.locator('.pet-mini-avatar-button')).toBeFocused();
});

for (const [name, query] of [['a running turn', 'running=1'], ['a pending Vault handoff', 'history=actions']] as const) {
  test(`${name} keeps the bubble shown`, async ({ page }) => {
    await page.goto(`/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&${query}`);
    await expect(bubbleShown(page)).toBeVisible();
    await page.waitForTimeout(500);
    await expect(bubbleShown(page)).toBeVisible();
  });
}

for (const theme of ['light', 'dark']) {
  test(`a Vibey click opens the history from a hidden bubble, and a second hides it again (${theme})`, async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&asr=1');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const avatar = page.locator('.pet-mini-avatar-button');
    await expect(avatar).toBeVisible();
    await expect(bubbleShown(page)).toHaveCount(0);
    await expect(avatar).toHaveAttribute('aria-label', '最近几轮');
    await expect(avatar).toHaveAttribute('aria-expanded', 'false');

    await avatar.click();
    await expect(bubbleShown(page)).toBeVisible();
    await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
    await expect(avatar).toHaveAttribute('aria-expanded', 'true');
    await expect.poll(() => page.locator('.pet-mini-bubble-history').evaluate((node) => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop))).toBeLessThanOrEqual(1);

    // Closing the history leaves nothing active, so the bubble hides at once.
    await avatar.click();
    await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).not.toHaveClass(/is-open/);
    await expect(bubbleShown(page)).toHaveCount(0);
    await expect(avatar).toHaveAttribute('aria-expanded', 'false');
  });
}

test('keyboard activation of Vibey toggles the history like a click', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh');
  const avatar = page.locator('.pet-mini-avatar-button');
  await avatar.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
  await expect(avatar).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('Enter');
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).not.toHaveClass(/is-open/);
  await expect(avatar).toHaveAttribute('aria-expanded', 'false');
});

test('a Vibey click with no conversation yet does nothing', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?main=false&activities=0');
  const avatar = page.locator('.pet-mini-avatar-button');
  await expect(avatar).toBeVisible();
  await avatar.click();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail.is-open')).toHaveCount(0);
  await expect(avatar).toHaveAttribute('aria-expanded', 'false');
});

test('holding Vibey still starts input, not the history', async ({ page }) => {
  // Voice is unavailable here, so a hold opens the text input instead of
  // dictation: the same hold path, without a microphone in the test browser.
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh');
  const avatar = page.locator('.pet-mini-avatar-button');
  const box = (await avatar.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(500);
  await page.mouse.up();
  await expect(page.getByRole('textbox')).toBeVisible();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail.is-open')).toHaveCount(0);
  // While typing, a Vibey click opens the history above the input and leaves
  // the input, and its draft, as they are (§5.1).
  await page.getByRole('textbox').fill('a draft');
  await avatar.click();
  await expect(page.getByRole('textbox')).toHaveValue('a draft');
  await expect(page.getByRole('textbox')).toBeFocused();
  await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail.is-open')).toHaveCount(1);
});

for (const theme of ['light', 'dark']) {
  for (const reduced of [false, true]) {
    test(`the history opens above a growing input and both fit the fixed window (${theme}, reduced=${reduced})`, async ({ page }) => {
      await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
      await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&activities=4');
      await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
      const avatar = page.locator('.pet-mini-avatar-button');
      await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
      const anchor = await avatar.boundingBox();
      await avatar.hover();
      await page.getByRole('button', { name: '输入消息', exact: true }).click();
      const input = page.getByRole('textbox');
      await input.fill('第一行');
      // The caret, mid-draft, survives the click: typing continues where it was.
      await input.evaluate((node: HTMLTextAreaElement) => node.setSelectionRange(2, 2));
      await avatar.click();
      await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
      await expect(input).toBeFocused();
      await page.keyboard.type('·');
      await expect(input).toHaveValue('第一·行');
      await input.evaluate((node: HTMLTextAreaElement) => node.setSelectionRange(node.value.length, node.value.length));
      // Grow the draft to the input's four-line cap while the history is open.
      for (const line of ['第二行', '第三行', '第四行', '第五行']) {
        await page.keyboard.press('Shift+Enter');
        await page.keyboard.type(line);
      }
      await expect(input).toHaveValue('第一·行\n第二行\n第三行\n第四行\n第五行');
      await settleMotion(page);
      const fits = async () => {
        const stack = (await page.locator('.pet-mini-cards').boundingBox())!;
        const bubble = (await page.locator('.pet-mini-reply-bubble').boundingBox())!;
        const history = (await page.locator('.pet-mini-bubble-history').boundingBox())!;
        const form = (await page.locator('.pet-mini-input').boundingBox())!;
        expect(stack.y).toBeGreaterThanOrEqual(0);
        expect(stack.y + stack.height).toBeLessThanOrEqual(bubble.y + 1);
        expect(history.y).toBeGreaterThanOrEqual(bubble.y - 1);
        expect(history.height).toBeGreaterThan(40);
        // The tail (7px) sits in the gap above the input, which ends above Vibey.
        expect(bubble.y + bubble.height + 7).toBeLessThanOrEqual(form.y + 1);
        expect(form.y + form.height).toBeLessThanOrEqual(anchor!.y + 4 + 1);
        expect(await avatar.boundingBox()).toEqual(anchor);
        // Both are desktop input regions.
        await expect.poll(() => page.evaluate(() => {
          const covered = (selector: string) => {
            const box = document.querySelector(selector)!.getBoundingClientRect();
            return window.petLayoutHitRegions.some((rect) => rect.x <= box.left + 1 && rect.y <= box.top + 1
              && rect.x + rect.width >= box.right - 1 && rect.y + rect.height >= box.bottom - 1);
          };
          return covered('.pet-mini-reply-bubble') && covered('.pet-mini-input');
        })).toBe(true);
      };
      await fits();
      // The history scrolls inside its shrunken box, ending at the bottom.
      const history = page.locator('.pet-mini-bubble-history');
      expect(await history.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true);
      await expect.poll(() => history.evaluate((node) => `${node.scrollTop} ${node.scrollHeight} ${node.clientHeight} ${Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) <= 1}`)).toMatch(/true$/);
      // Escape closes the history first, then the input.
      await page.keyboard.press('Escape');
      await expect(page.locator('.pet-mini-detail.is-open')).toHaveCount(0);
      await expect(input).toHaveValue('第一·行\n第二行\n第三行\n第四行\n第五行');
      await page.keyboard.press('Escape');
      await expect(page.getByRole('textbox')).toHaveCount(0);
      expect(await page.evaluate(() => window.petFixtureSends)).toEqual([]);
    });
  }
}

test('a double-click opens the conversation in Avibe and leaves the history as it was', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long');
  const avatar = page.locator('.pet-mini-avatar-button');
  await expect(avatar).toBeVisible();
  await avatar.dblclick();
  await expect.poll(() => page.evaluate(() => window.petFixtureOpens)).toEqual(['avibe://session/ses_main']);
  await page.waitForTimeout(100);
  await expect(avatar).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('.pet-mini-detail.is-open')).toHaveCount(0);
  await avatar.click();
  await expect(page.locator('.pet-mini-detail.is-open')).toHaveCount(1);
  await avatar.dblclick();
  await expect.poll(() => page.evaluate(() => window.petFixtureOpens.length)).toBe(2);
  await page.waitForTimeout(100);
  await expect(avatar).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('.pet-mini-detail.is-open')).toHaveCount(1);
  // A drag never opens it.
  const box = (await avatar.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 20, box.y + box.height / 2 + 5, { steps: 4 });
  await page.mouse.up();
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => window.petFixtureOpens.length)).toBe(2);
});

for (const reduced of [false, true]) {
  test(`thinking is content-sized and centred, and the reply widens it from the centre (reduced=${reduced})`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&running=1');
    await expect(page.locator('.pet-mini-reply-toggle')).toContainText('思考中');
    await settleMotion(page);
    const avatar = (await page.locator('.pet-mini-avatar-button').boundingBox())!;
    const centre = avatar.x + avatar.width / 2;
    const full = await page.locator('.pet-mini-slot-body').evaluate((node) => node.clientWidth - 36);
    const narrow = (await page.locator('.pet-mini-reply-bubble').boundingBox())!;
    expect(narrow.width).toBeGreaterThanOrEqual(160 - 0.5);
    expect(narrow.width).toBeLessThan(full / 2);
    expect(Math.abs(narrow.x + narrow.width / 2 - centre)).toBeLessThanOrEqual(1);
    const frames = await page.evaluate(async () => {
      const node = document.querySelector('.pet-mini-reply-bubble')!;
      window.petFixtureFinishTurn({ id: 'live_reply', author: 'agent', type: 'result', text: '做完了，细节都整理好了。', read_at: null });
      const seen: { width: number; centre: number }[] = [];
      const start = performance.now();
      while (performance.now() - start < 450) {
        await new Promise((resolve) => requestAnimationFrame(resolve));
        const box = node.getBoundingClientRect();
        seen.push({ width: box.width, centre: box.left + box.width / 2 });
      }
      return seen;
    });
    await expect(page.locator('.pet-mini-reply-toggle')).toContainText('已回复');
    for (const frame of frames) expect(Math.abs(frame.centre - centre)).toBeLessThanOrEqual(1);
    const widths = frames.map((frame) => frame.width);
    for (let i = 1; i < widths.length; i += 1) expect(widths[i]).toBeGreaterThanOrEqual(widths[i - 1] - 0.5);
    expect(widths.at(-1)).toBeCloseTo(full, 0);
    const between = widths.filter((width) => width > narrow.width + 2 && width < full - 2);
    if (reduced) expect(between).toEqual([]);
    else expect(between.length).toBeGreaterThan(2);
    expect(await page.locator('.pet-mini-avatar-button').boundingBox()).toEqual(avatar);
  });
}

for (const cards of [1, 3]) {
  test(`with ${cards} card${cards > 1 ? 's' : ''}, the bubble hidden, shown or open fits without overlap`, async ({ page }) => {
    const fits = async () => {
      await settleMotion(page);
      const cardsBox = (await page.locator('.pet-mini-cards').boundingBox())!;
      const avatar = (await page.locator('.pet-mini-avatar-button').boundingBox())!;
      expect(cardsBox.y).toBeGreaterThanOrEqual(0);
      expect(avatar.y + avatar.height).toBeLessThanOrEqual(600);
      if (await bubbleShown(page).count()) {
        const bubble = (await page.locator('.pet-mini-reply-bubble').boundingBox())!;
        expect(cardsBox.y + cardsBox.height).toBeLessThanOrEqual(bubble.y + 1);
        expect(bubble.y + bubble.height).toBeLessThanOrEqual(600);
      } else {
        expect(cardsBox.y + cardsBox.height).toBeLessThanOrEqual(avatar.y + avatar.height);
      }
    };
    // Hidden, then opened by a Vibey click.
    await page.goto(`/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&activities=${cards}&history=long`);
    await expect(page.locator('.pet-mini-avatar-button')).toBeVisible();
    await expect(bubbleShown(page)).toHaveCount(0);
    await fits();
    await page.locator('.pet-mini-avatar-button').click();
    await expect(page.locator('.pet-mini-reply-bubble .pet-mini-detail')).toHaveClass(/is-open/);
    await fits();
    // Shown with its summary while Vibey works.
    await page.goto(`/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&activities=${cards}&history=long&running=1`);
    await expect(bubbleShown(page)).toBeVisible();
    await fits();
  });
}

for (const asr of [true, false]) {
  test(`the dock holds ${asr ? 'mic and keyboard' : 'the keyboard alone'} (voice ${asr ? 'available' : 'unavailable'})`, async ({ page }) => {
    await page.goto(`/e2e/pet-mini/fixture.html?scenario=personal&lang=zh${asr ? '&asr=1' : ''}`);
    await page.locator('.pet-mini-avatar-button').hover();
    const dock = page.locator('.pet-mini-dock');
    await expect(dock).toHaveCSS('opacity', '1');
    if (asr) await expect(dock.getByRole('button', { name: '语音输入', exact: true })).toBeVisible();
    const order = await dock.evaluate((node) => [...node.children].map((child) => child.tagName === 'BUTTON' ? child.getAttribute('aria-label') : child.classList.contains('pet-mini-dock-divider') ? '|' : null).filter(Boolean));
    expect(order).toEqual(asr ? ['语音输入', '|', '输入消息'] : ['输入消息']);
  });
}

// Spec §9.1: near the top of the display the shell flips the window; Vibey and
// the dock sit at its top and the column opens below them, mirrored.
for (const theme of ['light', 'dark']) {
  test(`the top layout opens the column below Vibey and the dock, mirrored (${theme})`, async ({ page }) => {
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&activities=3&edge=top');
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    const avatar = page.locator('.pet-mini-avatar-button');
    await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
    await settleMotion(page);
    const anchor = (await avatar.boundingBox())!;
    expect(anchor.y).toBeCloseTo(12, 0);
    expect(anchor.x + anchor.width / 2).toBeCloseTo(230, 0);
    const below = 163 - 7; // the column's top, less the bubble's tail
    const fits = async (selector: string) => {
      const box = (await page.locator(selector).first().boundingBox())!;
      expect(box.y).toBeGreaterThanOrEqual(below - 1);
      expect(box.y + box.height).toBeLessThanOrEqual(600 + 1);
      return box;
    };
    // Collapsed: the layers peek out above the front card, toward Vibey.
    const front = await fits('.pet-mini-front');
    const layer = (await page.locator('.pet-mini-layer').first().boundingBox())!;
    expect(layer.y).toBeLessThan(front.y);

    // History: the toggle row is nearest Vibey, the history opens below it,
    // the tail points up, and the stack stays below the bubble.
    await avatar.click();
    await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
    await settleMotion(page);
    const toggle = await fits('.pet-mini-reply-toggle');
    const history = await fits('.pet-mini-bubble-history');
    const bubble = await fits('.pet-mini-reply-bubble');
    const stack = await fits('.pet-mini-collapsed');
    expect(toggle.y).toBeLessThan(history.y);
    expect(bubble.y + bubble.height).toBeLessThanOrEqual(stack.y + 1);
    expect(await page.locator('.pet-mini-reply-bubble').evaluate((node) => getComputedStyle(node, '::after').top)).toBe('-7px');
    const open = (await page.locator('.pet-mini-history-open').boundingBox())!;
    expect(open.y).toBeGreaterThan(history.y + history.height / 2);
    await expect.poll(() => bubbleIsRegion(page)).toBe(true);

    // The list opens below the speech slot, inside the window.
    await page.locator('.pet-mini-collapsed').click();
    await expect(page.locator('.pet-mini-scroll')).toBeVisible();
    await settleMotion(page);
    await fits('.pet-mini-scroll');
    await avatar.click();
    await expect(page.locator('.pet-mini-collapsed')).toBeVisible();

    // Input with the history: the input is nearest Vibey, the bubble below it.
    await avatar.hover();
    await page.getByRole('button', { name: '输入消息', exact: true }).click();
    await page.getByRole('textbox').fill('帮我看一下这周的进展');
    await avatar.click();
    await expect(page.locator('.pet-mini-detail')).toHaveClass(/is-open/);
    await settleMotion(page);
    const input = await fits('.pet-mini-input');
    const over = await fits('.pet-mini-reply-bubble');
    expect(input.y + input.height + 7).toBeLessThanOrEqual(over.y + 1);
    await fits('.pet-mini-collapsed');
    expect(await avatar.boundingBox()).toEqual(anchor);
  });
}
