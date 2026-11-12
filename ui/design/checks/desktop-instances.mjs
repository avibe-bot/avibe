// Static design contract: run with `node design/checks/desktop-instances.mjs` from ui/.
// Uses the same Vite/Playwright fixture canvas as design:shot; no native bridge or live API.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
import { createServer } from 'vite';

const root = fileURLToPath(new URL('../../', import.meta.url));
const server = await createServer({ root, logLevel: 'error', server: { port: 0 } });
await server.listen();
const browser = await chromium.launch();
const context = await browser.newContext();
const results = [];

try {
  const base = server.resolvedUrls.local[0];
  const list = await context.newPage();
  await list.goto(`${base}design.html?list`);
  await list.waitForFunction(() => window.__DESIGN__);
  const frames = await list.evaluate(() => window.__DESIGN__.frames.filter(frame => frame.id.startsWith('desktop/instances/')));
  await list.close();
  assert.ok(frames.length, 'instance frames must be discovered');
  const tasks = frames.flatMap(frame => (frame.themes ?? ['light', 'dark']).map(theme => ({ frame, theme })));
  let next = 0;
  const workers = await Promise.allSettled(Array.from({ length: 4 }, async () => {
    while (next < tasks.length) {
      const { frame, theme } = tasks[next++];
      const page = await context.newPage();
      try {
        await page.setViewportSize({ width: frame.width, height: frame.height });
        await page.goto(`${base}design.html?frame=${encodeURIComponent(frame.id)}&theme=${theme}`);
        await page.waitForSelector('body[data-design-ready]', { timeout: 15_000 });
        const actual = await page.evaluate(() => {
          const rect = element => {
            const box = element.getBoundingClientRect();
            return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
          };
          return {
            entries: [...document.querySelectorAll('[data-placement-entry="bottom"]')].map(entry => {
              const indicator = entry.querySelector('[data-status-indicator]');
              const label = entry.querySelector('[data-account-label]');
              const chevron = entry.querySelector('[data-account-chevron]');
              const update = entry.querySelector('[data-update-trigger]');
              return {
                connection: entry.dataset.connectionStatus,
                available: entry.dataset.updateAvailable,
                compact: entry.dataset.compact,
                avatarX: rect(entry.querySelector('[data-account-avatar]')).left,
                updatePresent: Boolean(update),
                updateVisible: update ? update.getClientRects().length > 0 && getComputedStyle(update).visibility === 'visible' : false,
                // Popup open state is independent too; compare the actual trigger contents/label.
                updateMarkup: update ? `${update.innerHTML}|${update.getAttribute('aria-label')}|${update.className}` : null,
                indicatorMarkup: indicator?.outerHTML,
                labelGap: label ? rect(indicator).left - rect(label).right : null,
                chevronGap: chevron ? rect(chevron).left - rect(indicator).right : null,
              };
            }),
            menus: [...document.querySelectorAll('[data-update-menu],[data-instance-menu]')].map(rect),
            shellMetrics: [...document.querySelectorAll('[data-placement-sidebar]')].filter(sidebar => sidebar.querySelector('[data-compact="false"]')).map(sidebar => {
              const tools = sidebar.querySelector('[data-sidebar-footer]').firstElementChild;
              const icon = tools.querySelector('svg.lucide-settings');
              return { paddingTop: getComputedStyle(sidebar).paddingTop, toolsHeight: tools.getBoundingClientRect().height, iconWidth: icon.getBoundingClientRect().width, iconHeight: icon.getBoundingClientRect().height };
            }),
            layers: [...document.querySelectorAll('[data-native-safe-strip],[data-update-menu],[data-instance-menu]')].map(element => getComputedStyle(element).zIndex),
            footerRows: [...document.querySelectorAll('[data-sidebar-footer]')].map(footer => footer.children.length),
            overflow: document.documentElement.scrollWidth > innerWidth,
            unfixtured: window.__DESIGN__.unfixtured(),
          };
        });
        const key = `${frame.id}.${theme}`;
        assert.equal(actual.entries.length, 1, `${key}: footer must be present`);
        for (const entry of actual.entries) {
          assert.ok(['true', 'false'].includes(entry.available), `${key}: explicit boolean fixture`);
          assert.equal(entry.updatePresent, entry.available === 'true', `${key}: update presence follows availability only`);
          assert.equal(entry.updateVisible, entry.available === 'true', `${key}: connection must not hide the update`);
          assert.equal(entry.avatarX, 16, `${key}: Apps-container alignment`);
          for (const gap of [entry.labelGap, entry.chevronGap]) {
            if (gap !== null) assert.ok(gap >= 0 && gap <= 8, `${key}: adjacent connection indicator/chevron (${gap}px)`);
          }
        }
        for (const box of actual.menus) {
          assert.ok(box.left >= 16 && box.top >= 0 && box.right <= frame.width && box.bottom <= frame.height, `${key}: popup within viewport`);
        }
        for (const metric of actual.shellMetrics) {
          assert.deepEqual(metric, { paddingTop: '10px', toolsHeight: 39, iconWidth: 18, iconHeight: 18 }, `${key}: spacing utilities preserve mirrored metrics`);
        }
        assert.ok(actual.layers.every(layer => layer === '60'), `${key}: standard stacking utility preserves layers`);
        assert.ok(actual.footerRows.every(rows => rows === 2), `${key}: two-row Workbench footer`);
        assert.equal(actual.overflow, false, `${key}: no horizontal overflow`);
        assert.deepEqual(actual.unfixtured, [], `${key}: fixture-only API reads`);
        results.push({ id: frame.id, theme, ...actual });
      } finally {
        await page.close();
      }
    }
  }));
  for (const worker of workers) if (worker.status === 'rejected') throw worker.reason;
  assert.equal(results.length, tasks.length);

  // Compare the rendered affordance across connection states, not just individual snapshots.
  const updateMarkup = new Map();
  const indicatorMarkup = new Map();
  const combinations = new Set();
  for (const result of results) {
    const lang = result.id.endsWith('-en') ? 'en' : 'zh';
    for (const entry of result.entries) {
      combinations.add(`${lang}:${result.theme}:${entry.connection}:${entry.available}`);
      if (entry.updatePresent) {
        const key = `${lang}:${result.theme}:${entry.compact}`;
        if (updateMarkup.has(key)) assert.equal(entry.updateMarkup, updateMarkup.get(key), `${result.id}: connection changes updater DOM`);
        else updateMarkup.set(key, entry.updateMarkup);
      }
      const key = `${lang}:${result.theme}:${entry.connection}`;
      if (indicatorMarkup.has(key)) assert.equal(entry.indicatorMarkup, indicatorMarkup.get(key), `${result.id}: update changes connection indicator`);
      else indicatorMarkup.set(key, entry.indicatorMarkup);
    }
  }
  for (const lang of ['zh', 'en']) for (const theme of ['light', 'dark']) {
    for (const connection of ['signed-in', 'signed-out', 'switching', 'offline']) {
      assert.ok(combinations.has(`${lang}:${theme}:${connection}:true`), `${lang}/${theme}: missing ${connection}+update fixture`);
    }
    assert.ok(combinations.has(`${lang}:${theme}:signed-in:false`), `${lang}/${theme}: normal version baseline`);
  }
  console.log(JSON.stringify({ captures: results.length, invariant: 'connection and local update availability are orthogonal', avatarX: 16, maxAdjacentGap: 8, passed: true }));
} finally {
  await context.close();
  await browser.close();
  await server.close();
}
