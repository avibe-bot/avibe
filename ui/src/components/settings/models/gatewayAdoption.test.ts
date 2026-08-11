import { describe, expect, it, vi } from 'vitest';

import { resumeGatewayAdoption } from './gatewayAdoption';
import type { AgentSupply, RuntimeDependency } from './types';

const agent = (mode: AgentSupply['mode']): AgentSupply => ({
  backend: 'claude',
  mode,
  menu_kind: 'fixed',
  sources: { order: [], eligibility: [] },
});

const runtime = (health: RuntimeDependency['status']['health']): RuntimeDependency => ({
  contract_version: 5,
  manifest: { name: 'cliproxyapi', version: '1', source_sha: 'a'.repeat(40), assets: [] },
  status: { installed_version: health === 'not_installed' ? null : '1', verified: health !== 'not_installed', health },
});

const api = (overrides: Partial<Parameters<typeof resumeGatewayAdoption>[0]> = {}) => ({
  listAgents: vi.fn().mockResolvedValue([agent('direct')]),
  getRuntimeStatus: vi.fn().mockResolvedValue(runtime('ok')),
  startRuntime: vi.fn().mockResolvedValue(runtime('ok')),
  setAgentMode: vi.fn().mockResolvedValue(agent('hub')),
  ...overrides,
});

describe('resumeGatewayAdoption', () => {
  it('treats degraded as already started and goes straight to the mode PATCH', async () => {
    const client = api({ getRuntimeStatus: vi.fn().mockResolvedValue(runtime('degraded')) });

    await expect(resumeGatewayAdoption(client, 'claude')).resolves.toMatchObject({ ok: true });

    expect(client.startRuntime).not.toHaveBeenCalled();
    expect(client.setAgentMode).toHaveBeenCalledWith('claude', 'hub');
  });

  it('returns the mode PATCH AgentSupply with its newly materialized native source', async () => {
    const adopted = { ...agent('hub'), sources: { order: ['src_native'], eligibility: [] } };
    const client = api({ setAgentMode: vi.fn().mockResolvedValue(adopted) });

    await expect(resumeGatewayAdoption(client, 'claude')).resolves.toMatchObject({
      ok: true,
      agent: { mode: 'hub', sources: { order: ['src_native'] } },
    });
  });

  it('starts an idle runtime before switching the one backend', async () => {
    const client = api({ getRuntimeStatus: vi.fn().mockResolvedValue(runtime('not_started')) });

    await expect(resumeGatewayAdoption(client, 'claude')).resolves.toMatchObject({ ok: true });

    expect(client.startRuntime).toHaveBeenCalledOnce();
    expect(client.setAgentMode).toHaveBeenCalledWith('claude', 'hub');
  });

  it('keeps an uninstalled failure in the install step and never sends the mode PATCH', async () => {
    const client = api({
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('not_installed')),
      startRuntime: vi.fn().mockRejectedValue(new TypeError('network')),
    });

    await expect(resumeGatewayAdoption(client, 'claude')).resolves.toEqual({
      ok: false,
      failure: { step: 'install', reason: 'transport' },
      runtime: runtime('not_installed'),
    });

    expect(client.getRuntimeStatus).toHaveBeenCalledTimes(2);
    expect(client.setAgentMode).not.toHaveBeenCalled();
  });

  it('re-reads agents after a lost mode response and accepts the committed hub mode', async () => {
    const client = api({
      listAgents: vi.fn()
        .mockResolvedValueOnce([agent('direct')])
        .mockResolvedValueOnce([agent('hub')]),
      setAgentMode: vi.fn().mockRejectedValue(new TypeError('response lost')),
    });

    await expect(resumeGatewayAdoption(client, 'claude')).resolves.toMatchObject({
      ok: true,
      agent: { mode: 'hub' },
    });

    expect(client.listAgents).toHaveBeenCalledTimes(2);
  });

  it('closes an already-committed retry without touching runtime or mode', async () => {
    const client = api({ listAgents: vi.fn().mockResolvedValue([agent('hub')]) });

    await expect(resumeGatewayAdoption(client, 'claude')).resolves.toMatchObject({ ok: true });

    expect(client.getRuntimeStatus).not.toHaveBeenCalled();
    expect(client.startRuntime).not.toHaveBeenCalled();
    expect(client.setAgentMode).not.toHaveBeenCalled();
  });
});
