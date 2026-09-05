import { randomUUID } from 'node:crypto';

import type { RecordedTurn, Source } from './support/api';
import { routableModels } from './support/api';
import { hub as copy } from './support/copy';
import { E2E_SOURCE_PREFIX, mockBaseUrl } from './support/env';
import { expect, requireMockUpstream, requireModelHub, requireRuntimeRunning, test } from './support/fixtures';
import { labelledButton } from './support/hub';
import { captureAgentChain, restoreAgentChain, restoreNativeSources } from './support/restore';

// Additional live-browser evidence, not a Vitest scenario-catalog entry.
test('MH-ROUTING-011 recorded request error is independent of current route and cleared by latest success', async ({ api, mock, hub, page }) => {
  test.setTimeout(180_000);
  const projectId = process.env.VIBE_E2E_TURN_PROJECT_ID?.trim();
  test.skip(!projectId, 'Set VIBE_E2E_TURN_PROJECT_ID to a task-owned disposable project on the explicitly consented regression instance.');
  await requireModelHub(api);
  await requireRuntimeRunning(api);
  await requireMockUpstream(mock);
  const backend = 'codex';
  const agent = (await api.agents()).find((entry) => entry.backend === backend);
  test.skip(!agent?.cli_present, 'The recorded-turn scenario requires the real Codex CLI on the regression instance; OpenCode has no exact turn attribution.');
  await api.read(`/api/projects/${encodeURIComponent(projectId!)}`);
  const model = routableModels(agent!)[0];
  expect(model).toBeDefined();
  const originalRoute = await captureAgentChain(api, { backend, model });
  const originalOrder = await api.defaultSourceOrder(backend);
  const sourceIds = new Set((await api.sources()).map((entry) => entry.id));
  const suffix = randomUUID().slice(0, 8);
  const agentName = `e2e-recorded-${suffix}`;
  const ownPrefix = `${E2E_SOURCE_PREFIX}${suffix}-`;
  let sessionId: string | null = null;
  let agentAttempted = false;
  let modeAttempted = false;
  let first: Source;
  let second: Source;
  try {
    await mock.configure({ auth: 'ok', protocol: 'openai_responses', models_endpoint: 'ok', models: [], model_errors: {}, stream: 'healthy' });
    const a = await api.createApiKeySource(`${ownPrefix}recorded-a`, `${mockBaseUrl()}/v1`);
    const b = await api.createApiKeySource(`${ownPrefix}recorded-b`, `${mockBaseUrl()}/v1`);
    expect(a).not.toBeNull();
    expect(b).not.toBeNull();
    first = a!;
    second = b!;
    expect(first.models).toEqual([]);
    expect(second.models).toEqual([]);
    if (agent!.mode !== 'hub') {
      modeAttempted = true;
      await api.setAgentMode(backend, 'hub');
    }
    await api.setDefaultSourceOrder(backend, [first.id]);
    expect(await api.deleteAgentChain(backend, model)).toBe(true);
    expect((await api.agentChain(backend, model)).manual_override).toBeNull();
    const healthBefore = (await api.sources()).find((entry) => entry.id === first.id)!.state;
    await mock.configure({ model_errors: { [model]: 'model_not_found' } });
    agentAttempted = true;
    await api.createTurnAgent(agentName, backend, model);
    sessionId = await api.createTurnSession(projectId!, agentName, model);
    await api.sendTurn(sessionId, 'Return the synthetic upstream response. Request: \u4e2d\u6587');
    let failed: RecordedTurn | null = null;
    await expect.poll(async () => {
      failed = await api.latestRecordedTurn(backend, model);
      return failed?.terminal_error?.source_id === first.id ? failed.terminal_error.upstream_error_code : null;
    }, { timeout: 70_000 }).toBe('model_not_found');
    const recorded = failed! as RecordedTurn;
    expect(recorded.terminal_error).toMatchObject({ source_id: first.id, configured_model_id: model, http_status: 404 });
    expect((await api.sources()).find((entry) => entry.id === first.id)!.state).toEqual(healthBefore);
    expect((await api.agentChain(backend, model)).route_origin).toBe('passthrough');

    // Changing today's route must not relabel the historical failing source.
    expect(await api.putAgentChain(backend, model, [{ source_id: second.id, model_id: model }])).toBe(true);
    await hub.goto();
    await hub.openRoute(backend, model);
    const dialog = hub.routeDialog;
    const recordedPanel = dialog.locator('.model-hub-recorded-turn');
    await expect(recordedPanel).toContainText(copy('routing.latestRecorded'));
    await expect(recordedPanel).toContainText(copy('routing.modelNotFound'));
    await expect(recordedPanel).toContainText('model_not_found');
    await expect(recordedPanel).toContainText(first.id);
    await expect(recordedPanel).toContainText(model);
    await expect(recordedPanel.locator('time')).toHaveAttribute('datetime', recorded.ts);
    await expect(dialog.locator('.model-hub-route-hop-name')).toHaveText([second.display_name]);
    expect(await api.latestRecordedTurn(backend, model)).toEqual(recorded);
    await labelledButton(recordedPanel, copy('routing.errorDetails')).click();
    const details = page.getByRole('dialog', { name: copy('routing.latestRecorded'), exact: true });
    await expect(details).toBeVisible();
    expect(JSON.parse(await details.locator('pre').innerText())).toEqual(recorded);
    await details.press('Escape');
    await expect(details).toHaveCount(0);
    await labelledButton(dialog, copy('routeDialog.cancel')).click();

    expect(await api.deleteAgentChain(backend, model)).toBe(true);
    await mock.configure({ model_errors: {} });
    await api.sendTurn(sessionId, 'Return the synthetic upstream response again.');
    await expect.poll(async () => {
      const latest = await api.latestRecordedTurn(backend, model);
      return latest?.turn_id !== recorded.turn_id && latest?.served?.source_id === first.id && latest.terminal_error === null;
    }, { timeout: 70_000 }).toBe(true);
    await page.reload();
    const historyRead = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/models/agents/${backend}/provenance`);
    await hub.openRoute(backend, model);
    expect((await historyRead).ok()).toBe(true);
    await expect(hub.routeDialog.locator('.model-hub-recorded-turn')).toHaveCount(0);
    expect((await api.agentChain(backend, model)).route_origin).toBe('passthrough');
    expect((await api.sources()).find((entry) => entry.id === first.id)!.state).toEqual(healthBefore);
  } finally {
    try {
      if (agentAttempted) await api.removeTurnFixture(agentName, sessionId);
    } finally {
      try {
        await restoreAgentChain(api, { backend, model }, originalRoute);
      } finally {
        try {
          await api.setDefaultSourceOrder(backend, originalOrder);
        } finally {
          try {
            if (modeAttempted) await api.setAgentMode(backend, agent!.mode);
          } finally {
            try {
              if (modeAttempted) await restoreNativeSources(api, sourceIds);
            } finally {
              try {
                for (const entry of await api.sources()) {
                  if (entry.display_name.startsWith(ownPrefix)) await api.deleteSource(entry.id);
                }
              } finally {
                await mock.configure({ model_errors: {}, stream: 'healthy' });
              }
            }
          }
        }
      }
    }
  }
});
