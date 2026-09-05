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
  contract_version: 9,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1', source_sha: 'a'.repeat(40), assets: [] },
  status: { installed_version: health === 'not_installed' ? null : '1', verified: health !== 'not_installed', health },
});

type GatewayClient = Pick<ModelsApi, 'listAgents' | 'getRuntimeStatus' | 'installRuntime' | 'startRuntime' | 'setAgentMode'>;

const api = (overrides: Partial<GatewayClient> = {}): GatewayClient => ({
  listAgents: vi.fn().mockResolvedValue([agent('direct')]),
  getRuntimeStatus: vi.fn().mockResolvedValue(runtime('ok')),
  installRuntime: vi.fn().mockResolvedValue(runtime('not_started')),
  startRuntime: vi.fn().mockResolvedValue(runtime('ok')),
  setAgentMode: vi.fn().mockResolvedValue(agent('hub')),
  ...overrides,
});

const adopt = (
  client: ReturnType<typeof api>,
  intervalMs?: number,
) => resumeGatewayAdoption(client, createAgentCollectionReadAuthority(client), 'claude', intervalMs);

describe('resumeGatewayAdoption', () => {
  it('treats degraded as already started and goes straight to the mode PATCH', async () => {
    const client = api({ getRuntimeStatus: vi.fn().mockResolvedValue(runtime('degraded')) });

    await expect(adopt(client)).resolves.toMatchObject({ ok: true });

    expect(client.startRuntime).not.toHaveBeenCalled();
    expect(client.setAgentMode).toHaveBeenCalledWith('claude', 'hub');
  });

  it('returns the mode PATCH AgentSupply with its newly materialized native source', async () => {
    const adopted = { ...agent('hub'), sources: { order: ['src_native'], eligibility: [] } };
    const client = api({ setAgentMode: vi.fn().mockResolvedValue(adopted) });

    await expect(adopt(client)).resolves.toMatchObject({
      ok: true,
      agent: { mode: 'hub', sources: { order: ['src_native'] } },
    });
  });

  it('starts an idle runtime before switching the one backend', async () => {
    const client = api({ getRuntimeStatus: vi.fn().mockResolvedValue(runtime('not_started')) });

    await expect(adopt(client)).resolves.toMatchObject({ ok: true });

    expect(client.startRuntime).toHaveBeenCalledOnce();
    expect(client.setAgentMode).toHaveBeenCalledWith('claude', 'hub');
  });

  it('installs, starts, and switches in the order of the first unproven step', async () => {
    const calls: string[] = [];
    const client = api({
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('not_installed')),
      installRuntime: vi.fn().mockImplementation(async () => { calls.push('install'); return runtime('not_started'); }),
      startRuntime: vi.fn().mockImplementation(async () => { calls.push('start'); return runtime('ok'); }),
      setAgentMode: vi.fn().mockImplementation(async () => { calls.push('mode'); return agent('hub'); }),
    });

    await expect(adopt(client, 0)).resolves.toMatchObject({ ok: true });

    expect(calls).toEqual(['install', 'start', 'mode']);
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

  it('re-reads agents after a lost mode response and accepts the committed hub mode', async () => {
    const client = api({
      listAgents: vi.fn()
        .mockResolvedValueOnce([agent('direct')])
        .mockResolvedValueOnce([agent('hub')]),
      setAgentMode: vi.fn().mockRejectedValue(new TypeError('response lost')),
    });

    await expect(adopt(client)).resolves.toMatchObject({
      ok: true,
      agent: { mode: 'hub' },
    });

    expect(client.listAgents).toHaveBeenCalledTimes(2);
  });

  it('closes an already-committed retry without touching runtime or mode', async () => {
    const client = api({ listAgents: vi.fn().mockResolvedValue([agent('hub')]) });

    await expect(adopt(client)).resolves.toMatchObject({ ok: true });

    expect(client.getRuntimeStatus).not.toHaveBeenCalled();
    expect(client.startRuntime).not.toHaveBeenCalled();
    expect(client.setAgentMode).not.toHaveBeenCalled();
  });
});
