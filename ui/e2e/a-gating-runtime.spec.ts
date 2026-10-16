// Suite A — the capability gate and the always-on gateway surface.
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
import {
  MODEL_HUB_DISABLED_REDIRECT,
  MODEL_HUB_SETTINGS_PATH,
} from '../src/components/settings/models/modelHubRoutes';
import { surfaceKind } from './support/api';
import { hub as copy } from './support/copy';
import { expect, requireModelHub, requireRuntimeRunning, test } from './support/fixtures';

test.describe('A · capability gate and runtime recovery', () => {
  test('A1 · the capability gate, not the browser, decides the route', async ({ page, hub, api }) => {
    const enabled = await api.modelHubEnabled();
    await hub.goto();

    if (enabled) {
      await expect(page).toHaveURL(new RegExp(`${MODEL_HUB_SETTINGS_PATH}$`));
      await expect(hub.shell).toBeVisible();
      await expect(page.getByRole('heading', { name: copy('shell.title'), level: 1 })).toBeVisible();
      await expect(hub.header.getByRole('switch')).toHaveCount(0);
      return;
    }
    await expect(page).toHaveURL(new RegExp(`${MODEL_HUB_DISABLED_REDIRECT}$`));
    await expect(hub.shell).toHaveCount(0);
  });

  test('A2 · a missing supported gateway offers recovery, not a runtime switch', async ({ hub, api }) => {
    await requireModelHub(api);
    const runtime = await api.runtime();
    test.skip(runtime?.status?.health !== 'not_installed', 'Requires a fresh hermetic instance with no engine installed.');
    test.skip(runtime?.manifest.resolution === 'unsupported', 'This platform has no compatible managed engine.');

    await hub.goto();
    await expect(hub.header.getByRole('switch')).toHaveCount(0);
    await expect(hub.runtimeRetry).toBeVisible();
    // Do not download into a shared instance. Install/start and failed-start
    // recovery are exercised through the same action in the isolated page tests.
  });

  test('A3 · a running gateway has no user-facing off switch', async ({ hub, api }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await hub.goto();
    await expect(hub.runtimePill).toBeVisible();
    await expect(hub.header.getByRole('switch')).toHaveCount(0);
    await expect(hub.closedState).toHaveCount(0);
    await expect(hub.tab('hub')).toBeVisible();
  });

  test('C6-direct-home · a machine with no sources lands on the direct home', async ({ hub, api }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    const surface = surfaceKind(await api.agents(), await api.sources());
    test.skip(surface === 'gateway', 'This instance already has sources or a Gateway backend.');
    test.skip(surface === 'direct-no-backend', 'No native CLI is installed, so the direct home shows its install prompt.');

    await hub.goto();
    await expect(hub.directHome).toBeVisible();
    await expect(hub.directHome).toContainText(copy('direct.card.current'));
    await expect(
      hub.directHome.getByRole('button', { name: copy('direct.action.switchToGateway') }).first(),
    ).toBeVisible();
  });
});
