import { mkdir } from 'node:fs/promises';
import { expect, test } from '../support/browserTest';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import { open, serveProduct } from './support';

// Full product page, fixture APIs, fresh Chrome profile; no live service access.
test.use({ channel: 'chrome' });
for (const width of [1200, 390]) {
  test(`global MCP Configure at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const denied = await serveProduct(page);
    await page.route('**/api/session', route => route.fulfill({ json: {
      remote: false, authenticated: true, authorization_state: 'current',
      instance_kind: 'personal', instance_role: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES,
    } }));
    let state = 'needs_config';
    const writes: unknown[] = [];
    await page.route('**/api/mcp**', async route => {
      const request = route.request();
      if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/mcp/docs/configure') {
        writes.push(request.postDataJSON());
        state = 'ok';
        return route.fulfill({ json: { ok: true, name: 'docs', toolCount: 1 } });
      }
      if (request.method() !== 'GET') throw new Error(`Unexpected write: ${request.url()}`);
      return route.fulfill({ json: { ok: true, servers: [{
        name: 'docs', source: 'avibe', scope: 'global', state, auth: 'none',
        transport: 'http', url: 'https://example.invalid/${TOKEN}',
        missingVariables: state === 'needs_config' ? ['TOKEN'] : [], placeholders: ['TOKEN'],
        toolCount: state === 'ok' ? 1 : 0, tools: state === 'ok' ? [{ name: 'search' }] : [],
      }] } });
    });
    await open(page, '/skills?tab=mcp');
    const row = page.getByRole('button', { name: /^docs/ });
    await expect(row).toBeVisible();
    await expect(row).toContainText('Needs config: TOKEN');
    await row.click();
    await page.getByRole('button', { name: 'Configure', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const input = dialog.getByLabel('Variables value');
    await expect(input).toHaveAttribute('type', 'password');
    await input.fill('fixture-token');
    await mkdir('e2e/.artifacts/mcp-placeholder', { recursive: true });
    await page.screenshot({ path: `e2e/.artifacts/mcp-placeholder/configure-${width}.png`, fullPage: true, animations: 'disabled' });
    const box = await dialog.boundingBox();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y + box!.height).toBeLessThanOrEqual(901);
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    await dialog.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(writes).toEqual([{ env: { TOKEN: 'fixture-token' } }]);
    await expect(page.getByText('search', { exact: true })).toBeVisible();
    await page.screenshot({ path: `e2e/.artifacts/mcp-placeholder/configured-${width}.png`, fullPage: true, animations: 'disabled' });
    expect(denied).toEqual([]);
  });
}
