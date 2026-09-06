import { randomUUID } from 'node:crypto';

import type { RecordedTurn, Source, TurnReceipt } from './support/api';
import { routableModels } from './support/api';
import { hub as copy } from './support/copy';
import { E2E_SOURCE_PREFIX, mockBaseUrl } from './support/env';
import { expect, requireMockUpstream, requireModelHub, requireRuntimeRunning, test } from './support/fixtures';
import { labelledButton } from './support/hub';
import { captureAgentChain, restoreAgentChain, restoreNativeSources } from './support/restore';

// Additional live-browser evidence, not a Vitest scenario-catalog entry.
test('MH-ROUTING-011 recorded request error is independent of current route and cleared by latest success', async ({ api, mock, hub, page }, testInfo) => {
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
  const keys = { A: `e2e-mh011-${randomUUID()}-a`, B: `e2e-mh011-${randomUUID()}-b` };
  let sessionId: string | null = null;
  let agentAttempted = false;
  let modeAttempted = false;
  let first: Source;
  let second: Source;
  let primaryFailure = false;
  let primaryError: unknown;
  const cleanupFailures: unknown[] = [];
  const cleanup = async (action: () => Promise<unknown>) => {
    try { await action(); } catch (error) { cleanupFailures.push(error); }
  };
  const observedReceipts: TurnReceipt[] = [];
  const observeTurn = async (label: string, text: string, expected: 'error' | 'served') => {
    let receipt: TurnReceipt | null = null;
    let latest: RecordedTurn | null = null;
    let primary: unknown;
    let failed = false;
    let requestLogReset = false;
    const requests: { method: string; path: string; model: unknown; synthetic_key_identity: string; uses_synthetic_b_key: boolean }[] = [];
    const evidenceFailures: string[] = [];
    try {
      await mock.resetRequests();
      requestLogReset = true;
      receipt = await api.sendTurn(sessionId!, text);
      observedReceipts.push(receipt);
      expect(receipt.queued).toBe(false);
      expect(new Set(observedReceipts.map((entry) => entry.turn_id)).size).toBe(observedReceipts.length);
      await expect.poll(async () => {
        latest = await api.latestRecordedTurn(backend, model);
        if (latest?.turn_id !== receipt!.turn_id) return false;
        return expected === 'error'
          ? latest.terminal_error?.source_id === first.id && latest.terminal_error.upstream_error_code === 'model_not_found'
          : latest.served?.source_id === first.id && latest.served.configured_model_id === model && latest.terminal_error === null;
      }, { timeout: 70_000 }).toBe(true);
    } catch (error) { failed = true; primary = error; }
    try {
      for (const entry of requestLogReset ? await mock.requests() : []) {
        const authorization = entry.headers?.authorization;
        const key = Object.entries(keys).find(([, value]) => authorization === `Bearer ${value}`)?.[0] ?? 'unrecognized';
        const body = entry.body as { model?: unknown } | null;
        requests.push({ method: entry.method, path: entry.path, model: typeof body?.model === 'string' ? body.model : null,
          synthetic_key_identity: key, uses_synthetic_b_key: authorization === `Bearer ${keys.B}` || entry.headers?.['x-api-key'] === keys.B });
      }
      expect(requests.filter((entry) => entry.uses_synthetic_b_key)).toEqual([]);
      const calls = requests.filter((entry) => entry.method === 'POST');
      expect(calls.length).toBeGreaterThan(0);
      expect(calls.every((entry) => entry.path === '/v1/responses' && entry.model === model && entry.synthetic_key_identity === 'A')).toBe(true);
    } catch (error) {
      evidenceFailures.push('mock request capture or exact model/source assertion failed');
      if (!failed) { failed = true; primary = error; }
    }
    try {
      // Only safe retained fields, never arbitrary headers, request bodies or receipt text.
      const record = latest as RecordedTurn | null;
      const retained = record && {
        turn_id: record.turn_id, ts: record.ts, agent: record.agent, requested_model_id: record.requested_model_id,
        terminal_error: record.terminal_error && {
          source_id: record.terminal_error.source_id, configured_model_id: record.terminal_error.configured_model_id,
          reason: record.terminal_error.reason, http_status: record.terminal_error.http_status,
          upstream_error_code: record.terminal_error.upstream_error_code,
        },
        served: record.served && { source_id: record.served.source_id, configured_model_id: record.served.configured_model_id },
      };
      await testInfo.attach(`MH-ROUTING-011-${label}`, { contentType: 'application/json', body: JSON.stringify({
        receipt, retained, requests, evidenceFailures, request_log_reset: requestLogReset,
        synthetic_sources: { A: { source_id: first.id, key: keys.A }, B: { source_id: second.id, key: keys.B } },
      }, null, 2) });
    } catch (error) { if (!failed) { failed = true; primary = error; } }
    if (failed) throw primary;
    return latest! as RecordedTurn;
  };
  try {
    await mock.configure({ auth: 'ok', protocol: 'openai_responses', models_endpoint: 'ok', models: [], model_errors: {}, stream: 'healthy' });
    const a = await api.createApiKeySource(`${ownPrefix}recorded-a`, `${mockBaseUrl()}/v1`, keys.A);
    const b = await api.createApiKeySource(`${ownPrefix}recorded-b`, `${mockBaseUrl()}/v1`, keys.B);
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
    const recorded = await observeTurn('failed-turn', 'Return the synthetic upstream response. Request: \u4e2d\u6587', 'error');
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
    await observeTurn('successful-turn', 'Return the synthetic upstream response again.', 'served');
    expect(observedReceipts).toHaveLength(2);
    await page.reload();
    const historyRead = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/models/agents/${backend}/provenance`);
    await hub.openRoute(backend, model);
    expect((await historyRead).ok()).toBe(true);
    await expect(hub.routeDialog.locator('.model-hub-recorded-turn')).toHaveCount(0);
    expect((await api.agentChain(backend, model)).route_origin).toBe('passthrough');
    expect((await api.sources()).find((entry) => entry.id === first.id)!.state).toEqual(healthBefore);
  } catch (error) {
    primaryFailure = true;
    primaryError = error;
  } finally {
    await cleanup(async () => { if (agentAttempted) await api.removeTurnFixture(agentName, sessionId); });
    await cleanup(() => restoreAgentChain(api, { backend, model }, originalRoute));
    await cleanup(() => api.setDefaultSourceOrder(backend, originalOrder));
    await cleanup(async () => { if (modeAttempted) await api.setAgentMode(backend, agent!.mode); });
    await cleanup(async () => { if (modeAttempted) await restoreNativeSources(api, sourceIds); });
    await cleanup(async () => {
      for (const entry of await api.sources()) if (entry.display_name.startsWith(ownPrefix)) {
        await cleanup(() => api.deleteSource(entry.id));
      }
    });
    await cleanup(() => mock.configure({ model_errors: {}, stream: 'healthy' }));
    await cleanup(() => testInfo.attach('MH-ROUTING-011-cleanup', { contentType: 'application/json',
      body: JSON.stringify({ failed: cleanupFailures.length > 0, failure_count: cleanupFailures.length, primary_failure_preserved: primaryFailure }) }));
  }
  if (primaryFailure) throw primaryError;
  if (cleanupFailures.length) throw new AggregateError(cleanupFailures, 'Recorded-turn cleanup failed');
});
