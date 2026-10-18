import { expect, test } from '@playwright/test';

for (const theme of ['light', 'dark']) {
  for (const lang of ['en', 'zh']) {
    test(`collapsed bubble spacing and action copy (${theme}, ${lang})`, async ({ page }) => {
      await page.goto(`/e2e/pet-mini/fixture.html?scenario=personal&lang=${lang}`);
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
      await page.goto(`/e2e/pet-mini/fixture.html?main=${main}&layers=${layers}`);
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

for (const cards of [1, 2, 12, 120]) {
  test(`shows the collapsed count for ${cards} cards`, async ({ page }) => {
    await page.goto(`/e2e/pet-mini/fixture.html?activities=${cards}&layers=1`);
    await expect(page.locator('.pet-mini-collapsed')).toBeVisible();
    const displayCount = cards > 99 ? '99+' : String(cards);
    const countChip = page.locator('.pet-mini-stack-count');
    if (cards === 1) {
      await expect(countChip).toHaveCount(0);
    } else {
      await expect(countChip).toHaveText(`${displayCount} items`);
    }
    const expectedLabel = cards === 1
      ? `Expand ${displayCount} session card`
      : `Expand ${displayCount} session cards`;
    await expect(page.locator('.pet-mini-collapsed')).toHaveAttribute(
      'aria-label',
      expectedLabel,
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
  await page.goto('/e2e/pet-mini/fixture.html?main=true&layers=2&sessions=4');
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
    const card = document.querySelector('.pet-mini-collapsed .pet-mini-card')!.getBoundingClientRect();
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
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal');
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
      await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=actions');
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
      await avatar.click();
      await expect(page.locator('.pet-mini-scroll')).toBeVisible();
      await expect(page.locator('.pet-mini-detail')).not.toHaveClass(/is-open/);
      expect(await avatar.boundingBox()).toEqual(anchor);
    });
  }
}


test('an empty activity stack takes no space while history remains usable without a switcher', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&activities=0');
  await page.locator('.pet-mini-reply-toggle').waitFor();
  const avatar = await page.locator('.pet-mini-avatar-button').boundingBox();
  await page.locator('.pet-mini-avatar-button').click();
  await expect(page.locator('.pet-mini-cards')).toHaveCSS('height', '0px');
  await expect(page.locator('.pet-mini-panel')).toHaveCount(0);
  await page.locator('.pet-mini-reply-toggle').click();
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
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long');
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
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long');
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
    expect(await icon.getAttribute('title')).toBeNull();
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
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&activities=8');
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
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long&unread=0');
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
});

test('a latest reply taller than the history opens at its end', async ({ page }) => {
  await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&reply=long');
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
    await page.goto('/e2e/pet-mini/fixture.html?scenario=personal&lang=zh&history=long');
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
    expect(await icon.getAttribute('title')).toBeNull();
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
    expect(await cardOpen.getAttribute('title')).toBeNull();
    await cardOpen.hover();
    await expect(tooltip).toHaveText('在 Avibe 中打开');
    const left = await unclipped();
    const card = (await cardOpen.boundingBox())!;
    expect(left.x + left.width).toBeLessThanOrEqual(card.x);
    await page.mouse.move(2, 2);
    await expect(tooltip).toHaveCount(0);
  });
}
