import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import type { AgentChain, HubApi } from './support/api';
import { HubApi as HubApiClient } from './support/api';
import { ModelHubPage } from './support/hub';
import { captureAgentChain, restoreAgentChain } from './support/restore';

type CleanupReply = { status: number; body: unknown };
const ownedSession = 'sess_owned';
const ownedAgent = 'e2e-recorded-owned';
const archivedSession = { id: ownedSession, status: 'archived' };

const acceptedTurn = { ok: true, session_id: ownedSession, turn_id: `trn_${'a'.repeat(32)}`, queued: false };
for (const [name, status, body, accepted] of [
  ['actual top-level receipt', 202, { ...acceptedTurn, text: 'must not persist', metadata: { private: true } }, true],
  ['queued receipt', 202, { ...acceptedTurn, queued: true }, true],
  ['missing turn id', 202, { ...acceptedTurn, turn_id: undefined }, false],
  ['invented nested turn envelope', 202, { ok: true, turn: { id: acceptedTurn.turn_id } }, false],
  ['wrong Session', 202, { ...acceptedTurn, session_id: 'sess_other' }, false],
  ['rejected response', 403, acceptedTurn, false],
  ['false ok', 202, { ...acceptedTurn, ok: false }, false],
  ['missing queued state', 202, { ...acceptedTurn, queued: undefined }, false],
] as const) {
  test(`sendTurn checks the real delivery envelope: ${name}`, async () => {
    const context = {
      get: async () => ({ ok: () => true, json: async () => ({ csrf_token: 'synthetic' }) }),
      post: async (path: string) => {
        expect(path).toBe(`/api/sessions/${ownedSession}/messages`);
        return { ok: () => status === 202, status: () => status, json: async () => body };
      },
    } as unknown as APIRequestContext;
    const sending = new HubApiClient(context).sendTurn(ownedSession, 'synthetic prompt');
    if (accepted) {
      expect(await sending).toEqual({ ...acceptedTurn, queued: body.queued });
    } else {
      await expect(sending).rejects.toThrow('exact accepted FSM receipt');
    }
  });
}

function turnCleanupClient(options: {
  turn?: 'completed' | 'active' | 'archived';
  archive?: CleanupReply;
  readback?: CleanupReply;
  agent?: CleanupReply;
} = {}) {
  const calls: string[] = [];
  const response = ({ status, body }: CleanupReply) => ({
    ok: () => status >= 200 && status < 300,
    status: () => status,
    statusText: () => 'Synthetic fixture response',
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  const request = {
    get: async (path: string) => {
      if (path === '/api/csrf-token') return response({ status: 200, body: { csrf_token: 'synthetic-csrf' } });
      calls.push(`GET ${path}`);
      expect(path).toBe(`/api/sessions/${ownedSession}`);
      return response(options.readback ?? { status: 200, body: archivedSession });
    },
    post: async (path: string) => {
      calls.push(`POST ${path}`);
      expect(path).toBe(`/api/sessions/${ownedSession}/cancel`);
      return response(options.turn === 'active'
        ? { status: 200, body: { ok: true } }
        : { status: 404, body: { ok: false, code: 'not_in_flight', session_id: ownedSession } });
    },
    delete: async (path: string) => {
      calls.push(`DELETE ${path}`);
      if (path === `/api/sessions/${ownedSession}`) {
        return response(options.archive ?? { status: 200, body: archivedSession });
      }
      expect(path).toBe(`/api/agents/${ownedAgent}`);
      return response(options.agent ?? { status: 200, body: { ok: true } });
    },
  } as unknown as APIRequestContext;
  return { client: new HubApiClient(request), calls };
}

test('completed turn cleanup archives and reads back without a cancel precondition', async () => {
  const { client, calls } = turnCleanupClient({ turn: 'completed' });
  await client.removeTurnFixture(ownedAgent, ownedSession);
  expect(calls).toEqual([
    `DELETE /api/sessions/${ownedSession}`,
    `GET /api/sessions/${ownedSession}`,
    `DELETE /api/agents/${ownedAgent}`,
  ]);
});

for (const turn of ['active', 'archived'] as const) {
  test(`${turn} turn cleanup uses the archive lifecycle and verifies persisted identity`, async () => {
    const { client, calls } = turnCleanupClient({ turn });
    await client.removeTurnFixture(ownedAgent, ownedSession);
    expect(calls).toEqual([
      `DELETE /api/sessions/${ownedSession}`,
      `GET /api/sessions/${ownedSession}`,
      `DELETE /api/agents/${ownedAgent}`,
    ]);
  });
}

for (const status of [403, 404, 500]) {
  test(`session archive rejects HTTP ${status} without a success waiver or retry`, async () => {
    const { client, calls } = turnCleanupClient({ archive: { status, body: { error: 'synthetic_refusal' } } });
    await expect(client.archiveTurnSession(ownedSession)).rejects.toThrow(`Archive fixture session failed: HTTP ${status}`);
    expect(calls).toEqual([`DELETE /api/sessions/${ownedSession}`]);
  });

  test(`session archive rejects HTTP ${status} from its readback`, async () => {
    const { client, calls } = turnCleanupClient({ readback: { status, body: { error: 'synthetic_refusal' } } });
    await expect(client.archiveTurnSession(ownedSession)).rejects.toThrow(`GET /api/sessions/${ownedSession}`);
    expect(calls).toEqual([`DELETE /api/sessions/${ownedSession}`, `GET /api/sessions/${ownedSession}`]);
  });
}

for (const [label, body] of [
  ['missing', null],
  ['different identity', { id: 'sess_someone_else', status: 'archived' }],
  ['still active', { id: ownedSession, status: 'active' }],
] as const) {
  test(`session archive rejects a successful response with ${label} state`, async () => {
    const { client, calls } = turnCleanupClient({ archive: { status: 200, body } });
    await expect(client.archiveTurnSession(ownedSession)).rejects.toThrow('response did not confirm archived identity');
    expect(calls).toEqual([`DELETE /api/sessions/${ownedSession}`]);
  });

  test(`session archive rejects a false-success readback with ${label} state`, async () => {
    const { client, calls } = turnCleanupClient({ readback: { status: 200, body } });
    await expect(client.archiveTurnSession(ownedSession)).rejects.toThrow('readback did not confirm archived identity');
    expect(calls).toEqual([`DELETE /api/sessions/${ownedSession}`, `GET /api/sessions/${ownedSession}`]);
  });
}

for (const archiveFails of [true, false]) {
  test(`Agent cleanup is attempted after ${archiveFails ? 'archive rejection' : 'false-success archive readback'}`, async () => {
    const { client, calls } = turnCleanupClient(archiveFails
      ? { archive: { status: 403, body: { error: 'forbidden' } } }
      : { readback: { status: 200, body: { id: ownedSession, status: 'active' } } });
    const failure = await client.removeTurnFixture(ownedAgent, ownedSession).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AggregateError);
    expect((failure as AggregateError).errors).toHaveLength(1);
    expect(calls.at(-1)).toBe(`DELETE /api/agents/${ownedAgent}`);
    expect(calls.some((call) => call.startsWith('POST '))).toBe(false);
  });
}

test('fixture cleanup reports both archive and Agent errors', async () => {
  const { client, calls } = turnCleanupClient({
    archive: { status: 404, body: { error: 'session_not_found' } },
    agent: { status: 500, body: { error: 'synthetic_agent_failure' } },
  });
  const failure = await client.removeTurnFixture(ownedAgent, ownedSession).catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(AggregateError);
  expect((failure as AggregateError).errors.map((error: Error) => error.message)).toEqual([
    'Archive fixture session failed: HTTP 404',
    'Remove fixture agent failed: {"error":"synthetic_agent_failure"}',
  ]);
  expect(calls).toEqual([`DELETE /api/sessions/${ownedSession}`, `DELETE /api/agents/${ownedAgent}`]);
});

test('Agent-only cleanup preserves the existing missing-Agent policy', async () => {
  const { client, calls } = turnCleanupClient({ agent: { status: 404, body: { error: 'agent_not_found' } } });
  await client.removeTurnFixture(ownedAgent, null);
  expect(calls).toEqual([`DELETE /api/agents/${ownedAgent}`]);
});

for (const override of [null, { hops: [] }, { hops: [{ source_id: 'src_operator', model_id: 'vendor/model' }] }]) {
  test(`route cleanup preserves canonical ${override === null ? 'absence' : override.hops.length ? 'manual hops' : 'empty inheritance'}`, async () => {
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
    const canonical = override?.hops.length ? override : null;
    expect(snapshot.manual_override).toEqual(canonical);
    saved = { hops: [{ source_id: 'src_test', model_id: 'test-target' }] };
    await restoreAgentChain(api, route, snapshot);
    expect(saved).toEqual(canonical);
    expect(writes).toEqual([canonical === null ? 'DELETE' : 'PUT']);
  });
}

for (const readback of [{ hops: [] }, { hops: [{ source_id: 'src_wrong', model_id: 'wrong-target' }] }, undefined]) {
  test(`route cleanup rejects noncanonical or false-success readback ${JSON.stringify(readback)}`, async () => {
    const api = {
      deleteAgentChain: async () => true,
      agentChain: async () => ({ manual_override: readback }),
    } as unknown as HubApi;
    await expect(restoreAgentChain(api, { backend: 'claude', model: 'menu-model' }, { manual_override: null }))
      .rejects.toThrow('Teardown must restore route intent');
  });
}

test('a legacy empty snapshot restores with DELETE and never recreates an empty manual value', async () => {
  const writes: string[] = [];
  const api = {
    deleteAgentChain: async () => { writes.push('DELETE'); return true; },
    putAgentChain: async () => { writes.push('PUT'); return true; },
    agentChain: async () => ({ manual_override: null }),
  } as unknown as HubApi;
  await restoreAgentChain(api, { backend: 'codex', model: 'menu-model' }, { manual_override: { hops: [] } });
  expect(writes).toEqual(['DELETE']);
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
