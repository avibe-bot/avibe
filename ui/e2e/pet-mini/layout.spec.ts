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
      for (const name of ['在 Avibe 中打开', '需要授权，在 Avibe 中打开', '继续']) {
        const button = history.getByRole('button', { name, exact: true });
        await expect(button).toBeVisible();
        const bounds = await button.boundingBox(), box = await history.boundingBox();
        expect(bounds!.y).toBeGreaterThanOrEqual(box!.y);
        expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(box!.y + box!.height + 1);
      }
      expect(await history.evaluate((node) => node.scrollHeight <= node.clientHeight + 1)).toBe(true);
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
