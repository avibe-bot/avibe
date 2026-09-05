import { expect, test, type Page } from '@playwright/test';

import type { AgentChain, HubApi } from './support/api';
import { HubApi as HubApiClient } from './support/api';
import { ModelHubPage } from './support/hub';
import { captureAgentChain, restoreAgentChain } from './support/restore';

for (const override of [null, { hops: [] }, { hops: [{ source_id: 'src_operator', model_id: 'vendor/model' }] }]) {
  test(`route cleanup preserves ${override === null ? 'absence' : override.hops.length ? 'manual hops' : 'explicit empty'}`, async () => {
    let saved: AgentChain['manual_override'] = override;
    const writes: string[] = [];
    const api = {
      sources: async () => [],
      agentChain: async () => ({ manual_override: saved, chain: [{ source_id: 'src_generated', model_id: 'generated' }] }),
      putAgentChain: async (_backend: string, _model: string, hops: NonNullable<AgentChain['manual_override']>['hops']) => {
        writes.push('PUT');
        saved = { hops };
        return true;
      },
      deleteAgentChain: async () => {
        writes.push('DELETE');
        saved = null;
        return true;
      },
    } as unknown as HubApi;
    const route = { backend: 'claude', model: 'menu-model' };
    const snapshot = await captureAgentChain(api, route);
    expect(snapshot.manual_override).toEqual(override);
    saved = { hops: [{ source_id: 'src_test', model_id: 'test-target' }] };
    await restoreAgentChain(api, route, snapshot);
    expect(saved).toEqual(override);
    expect(writes).toEqual([override === null ? 'DELETE' : 'PUT']);
  });
}

test('route cleanup rejects successful responses whose persisted intent was not restored', async () => {
  const api = {
    deleteAgentChain: async () => true,
    agentChain: async () => ({ manual_override: { hops: [] } }),
  } as unknown as HubApi;
  await expect(restoreAgentChain(api, { backend: 'claude', model: 'menu-model' }, { manual_override: null }))
    .rejects.toThrow('Teardown must restore route intent');
});

test('route cleanup preserves existing fixture-source references verbatim', async () => {
  const hops = [{ source_id: 'src_previous_fixture', model_id: 'vendor/model' }];
  const api = {
    sources: async () => [{ id: 'src_previous_fixture', display_name: 'e2e-playwright-from-earlier-run' }],
    agentChain: async () => ({ manual_override: { hops } }),
  } as unknown as HubApi;
  const snapshot = await captureAgentChain(api, { backend: 'codex', model: 'menu-model' });
  expect(snapshot.manual_override).toEqual({ hops });
});

test('gateway fixture cleanup keeps every source that preceded this test', async () => {
  const removed: string[] = [];
  const client = {
    modelHubEnabled: async () => true,
    sources: async () => [
      { id: 'src_previous_fixture', display_name: 'e2e-playwright-from-earlier-run' },
      { id: 'src_this_fixture', display_name: 'e2e-playwright-from-this-run' },
      { id: 'src_operator', display_name: 'Operator source' },
    ],
    deleteSource: async (id: string) => { removed.push(id); },
  } as unknown as HubApi;
  await HubApiClient.prototype.removeSuiteSources.call(client, new Set(['src_previous_fixture', 'src_operator']));
  expect(removed).toEqual(['src_this_fixture']);
});

for (const collapsed of [false, true]) {
  test(`route opening targets the model command, not the badge-covered row (${collapsed ? 'collapsed' : 'visible'})`, async () => {
    let expanded = !collapsed;
    let opened = false;
    let helpOpened = false;
    const commands: string[] = [];
    const opener = {
      focus: async () => { commands.push('focus model opener'); },
      press: async (key: string) => {
        expect(key).toBe('Enter');
        expect(commands.at(-1)).toBe('focus model opener');
        commands.push('activate model opener');
        opened = true;
      },
    };
    const row = {
      count: async () => expanded ? 1 : 0,
      // Models the live failure: the whole-row center activates its separate badge.
      click: async () => { helpOpened = true; },
      locator: (selector: string) => {
        expect(selector).toBe('button.model-hub-model-open');
        return opener;
      },
    };
    const card = {
      waitFor: async () => {},
      locator: (selector: string) => {
        expect(selector).toBe('.model-hub-model-collapse');
        return { first: () => ({ count: async () => 1, click: async () => { expanded = true; commands.push('expand models'); } }) };
      },
    };
    const page = {
      locator: (selector: string) => {
        if (selector === '[data-agent-backend="claude"]') return card;
        expect(selector).toBe('[data-route-backend="claude"][data-route-model="model-id"]');
        return row;
      },
    } as unknown as Page;

    await new ModelHubPage(page).openRoute('claude', 'model-id');

    expect(opened).toBe(true);
    expect(helpOpened).toBe(false);
    expect(commands).toEqual([
      ...(collapsed ? ['expand models'] : []), 'focus model opener', 'activate model opener',
    ]);
  });
}
