import { describe, expect, it, vi } from 'vitest';

import { createAgentCollectionReadAuthority } from './collectionReadAuthority';
import { resumeGatewayAdoption } from './gatewayAdoption';
import type { ModelsApi } from './modelsApi';
import type { AgentSupply, RuntimeDependency } from './types';

const agent = (mode: AgentSupply['mode']): AgentSupply => ({
  backend: 'claude',
  cli_present: true,
  mode,
  menu_kind: 'fixed',
  sources: { order: [], eligibility: [] },
});

const runtime = (health: RuntimeDependency['status']['health']): RuntimeDependency => ({
  contract_version: 10,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1', source_sha: 'a'.repeat(40), assets: [] },
  status: { installed_version: health === 'not_installed' ? null : '1', verified: health !== 'not_installed', health },
});

type GatewayClient = Pick<
  ModelsApi,
  'listAgents' | 'getRuntimeStatus' | 'installRuntime' | 'startRuntime' | 'scanMigration' | 'applyMigration' | 'setAgentMode'
>;

const api = (overrides: Partial<GatewayClient> = {}): GatewayClient => ({
  listAgents: vi.fn().mockResolvedValue([agent('direct')]),
  getRuntimeStatus: vi.fn().mockResolvedValue(runtime('ok')),
  installRuntime: vi.fn().mockResolvedValue(runtime('not_started')),
  startRuntime: vi.fn().mockResolvedValue(runtime('ok')),
  scanMigration: vi.fn().mockResolvedValue({ items: [] }),
  applyMigration: vi.fn().mockResolvedValue({ applied: 0, sources: [] }),
  setAgentMode: vi.fn().mockResolvedValue(agent('hub')),
  ...overrides,
});

const adopt = (
  client: ReturnType<typeof api>,
  intervalMs?: number,
) => resumeGatewayAdoption(client, createAgentCollectionReadAuthority(client), 'claude', intervalMs);

describe('resumeGatewayAdoption', () => {
  it('treats degraded as already started without changing backend mode', async () => {
    const client = api({ getRuntimeStatus: vi.fn().mockResolvedValue(runtime('degraded')) });

    await expect(adopt(client)).resolves.toMatchObject({ ok: true });

    expect(client.startRuntime).not.toHaveBeenCalled();
    expect(client.scanMigration).toHaveBeenCalledOnce();
    expect(client.applyMigration).not.toHaveBeenCalled();
    expect(client.setAgentMode).not.toHaveBeenCalled();
  });

  it('returns only the carried native rows for the selection dialog', async () => {
    const client = api({
      scanMigration: vi.fn().mockResolvedValue({
        items: [
          { id: 'oauth', backend: 'claude', kind: 'oauth_native', masked_detail: 'OAuth', proposed_action: 'import', selected: true },
          { id: 'keep', backend: 'claude', kind: 'oauth_native', masked_detail: 'Native', proposed_action: 'keep_native', selected: true },
          { id: 'other', backend: 'codex', kind: 'api_key', masked_detail: 'Key', proposed_action: 'import', selected: true },
        ],
      }),
    });

    await expect(adopt(client)).resolves.toMatchObject({
      ok: true,
      candidates: [{ id: 'oauth' }],
    });
    expect(client.applyMigration).not.toHaveBeenCalled();
    expect(client.setAgentMode).not.toHaveBeenCalled();
  });

  it('starts an idle runtime before scanning the one backend', async () => {
    const client = api({ getRuntimeStatus: vi.fn().mockResolvedValue(runtime('not_started')) });

    await expect(adopt(client)).resolves.toMatchObject({ ok: true });

    expect(client.startRuntime).toHaveBeenCalledOnce();
    expect(client.scanMigration).toHaveBeenCalledOnce();
  });

  it('installs, starts, and scans in the order of the first unproven step', async () => {
    const calls: string[] = [];
    const client = api({
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('not_installed')),
      installRuntime: vi.fn().mockImplementation(async () => { calls.push('install'); return runtime('not_started'); }),
      startRuntime: vi.fn().mockImplementation(async () => { calls.push('start'); return runtime('ok'); }),
      scanMigration: vi.fn().mockImplementation(async () => { calls.push('scan'); return { items: [] }; }),
    });

    await expect(adopt(client, 0)).resolves.toMatchObject({ ok: true });

    expect(calls).toEqual(['install', 'start', 'scan']);
  });

  it('keeps a terminal install failure in the install step and never sends the mode PATCH', async () => {
    const client = api({
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('not_installed')),
      installRuntime: vi.fn().mockResolvedValue(runtime('not_installed')),
    });

    await expect(adopt(client)).resolves.toEqual({
      ok: false,
      failure: { step: 'install', reason: 'unknown' },
      runtime: runtime('not_installed'),
    });

    expect(client.startRuntime).not.toHaveBeenCalled();
    expect(client.setAgentMode).not.toHaveBeenCalled();
  });

  it('keeps Direct mode when the migration scan fails', async () => {
    const client = api({
      scanMigration: vi.fn().mockRejectedValue(new TypeError('response lost')),
    });

    await expect(adopt(client)).resolves.toMatchObject({
      ok: false,
      failure: { step: 'scan' },
    });
    expect(client.applyMigration).not.toHaveBeenCalled();
    expect(client.setAgentMode).not.toHaveBeenCalled();
  });
});
