import { expect, test, type Locator, type Page } from '@playwright/test';

import { open, serveProduct } from './support';

const MOBILE_SHORT = { width: 390, height: 500 };
const DESKTOP_SHORT = { width: 1200, height: 500 };
const REGISTRY_SKILLS = Array.from({ length: 28 }, (_, index) => ({
  id: `registry-${index}`,
  name: `skill-with-a-deliberately-long-name-${index}-xxxxxxxxxxxxxxxx`,
  description: 'A registry fixture with enough content to exercise the dialog scroll boundary.',
  owner: 'owner-with-a-long-unbroken-identifier',
  repo: 'repository-with-a-long-unbroken-identifier',
  tags: ['tag-with-a-long-unbroken-identifier'],
  stars: index,
  aiScore: 0.9,
  aiBreakdown: [],
  updatedAt: null,
  installSource: `gh:fixture/repository@skill-${index}`,
  url: null,
}));
const DISCOVERED_SKILLS = Array.from({ length: 26 }, (_, index) => ({
  name: `discovered-skill-with-a-deliberately-long-name-${index}-xxxxxxxxxxxxxxxx`,
  description: 'A discovered skill with a long description used to force the Add Skill dialog to scroll.',
}));

const SESSION = {
  remote: false,
  authenticated: true,
  authorization_state: 'current',
  instance_kind: 'personal',
  instance_role: 'owner',
  capabilities: {
    is_instance_owner: true,
    can_read_instance: true,
    can_chat: true,
    can_manage_projects: true,
    can_manage_agents: true,
    can_manage_instance: true,
    can_manage_access_members: true,
    can_use_agents: true,
    can_use_skills: true,
    can_use_vault_secrets: true,
    can_use_show_pages: true,
    can_use_terminal_files: true,
    can_use_terminal: true,
    can_use_files: true,
    can_use_system: true,
  },
};

async function serveSkills(page: Page) {
  const denied = await serveProduct(page);
  await page.route('**/api/session', (route) => route.fulfill({ json: SESSION }));
  await page.route('**/api/skills**', (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/skills' && request.method() === 'GET') {
      return route.fulfill({ json: { ok: true, skills: [] } });
    }
    if (path === '/api/skills/check' && request.method() === 'GET') {
      return route.fulfill({ json: { ok: true, skills: [] } });
    }
    if (path === '/api/skills/find' && request.method() === 'GET') {
      return route.fulfill({ json: { ok: true, count: REGISTRY_SKILLS.length, skills: REGISTRY_SKILLS } });
    }
    if (path === '/api/skills/preview' && request.method() === 'POST') {
      return route.fulfill({ json: { ok: true, skills: DISCOVERED_SKILLS } });
    }
    return route.fallback();
  });
  return denied;
}

async function expectScrollableWithinViewport(dialog: Locator, page: Page, inset: number) {
  const shell = dialog;
  const viewport = page.viewportSize()!;
  const box = await shell.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(inset);
  expect(box!.y).toBeGreaterThanOrEqual(inset);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width - inset);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height - inset);

  const metrics = await shell.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowX: style.overflowX,
      overflowY: style.overflowY,
    };
  });
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  expect(metrics.overflowX).toBe('hidden');
  expect(metrics.overflowY).toBe('auto');

  await shell.evaluate((element) => element.scrollTo(0, element.scrollHeight));
  return { shell, viewport };
}

async function expectBottomActionVisible(button: Locator, viewport: { width: number; height: number }) {
  const box = await button.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
}

test('custom skill dialogs keep their actions reachable on short viewports', async ({ page }) => {
  const denied = await serveSkills(page);
  await page.setViewportSize(MOBILE_SHORT);
  await open(page, '/skills');

  await page.getByRole('button', { name: 'Browse registry', exact: true }).click();
  const browse = page.getByRole('dialog').filter({ hasText: 'Browse the askill registry' });
  await expect(browse).toBeVisible();
  await expect(page.getByRole('button', { name: 'Add', exact: true })).toHaveCount(REGISTRY_SKILLS.length);
  await expectScrollableWithinViewport(browse, page, 16);
  await expectBottomActionVisible(browse.getByRole('button', { name: 'Done', exact: true }), MOBILE_SHORT);
  await browse.getByRole('button', { name: 'Done', exact: true }).click();

  await page.getByRole('button', { name: 'Add skill', exact: true }).click();
  const add = page.getByRole('dialog').filter({ hasText: 'Add a skill' });
  await expect(add).toBeVisible();
  await add.getByPlaceholder('github.com/owner/repo or gh:owner/repo@skill').fill('github.com/fixture/repository');
  await add.getByRole('button', { name: 'Fetch', exact: true }).click();
  await expect(add.getByRole('checkbox')).toHaveCount(DISCOVERED_SKILLS.length);
  await expectScrollableWithinViewport(add, page, 16);
  await expectBottomActionVisible(add.getByRole('button', { name: /Install \d+ skill\(s\)/ }), MOBILE_SHORT);

  await page.setViewportSize(DESKTOP_SHORT);
  await expectScrollableWithinViewport(add, page, 40);
  await expectBottomActionVisible(add.getByRole('button', { name: /Install \d+ skill\(s\)/ }), DESKTOP_SHORT);
  expect(denied).toEqual([]);
});
