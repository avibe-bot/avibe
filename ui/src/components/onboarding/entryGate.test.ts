import { describe, expect, it, vi } from 'vitest';
import type { BackendConnectionState, VibeAgentBrief } from '../../context/ApiContext';
import { CONTRACT_VERSION, type AgentSupply, type RuntimeDependency } from '../settings/models/types';
import {
  admitEntry,
  chooseEntryDefault,
  configuredCliPath,
  entryCandidates,
  readEntryEvidence,
  type EntryEvidence,
  type EntryGateDeps,
} from './entryGate';
import type { AgentBackendId } from '@/lib/agentBackends';
import { SETUP_LINEUP } from './collaborationTimeline';

const RUNTIME_OK: RuntimeDependency = {
  contract_version: CONTRACT_VERSION,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'sha', assets: [] },
  status: { verified: true, health: 'ok' },
};

const CLAUDE: VibeAgentBrief = {
  id: 'cl-1', name: 'claude-agent', backend: 'claude', display_name: 'claude-agent',
  description: null, model: null, reasoning_effort: null, enabled: true, archived: false,
  archived_at: null, source: 'file', updated_at: '',
};

const claudeSupply = (overrides: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed',
  named_agents: [{ name: 'claude-agent', effective_model_id: 'openai/gpt-5.6-sol', supply_status: 'ok' }],
  ...overrides,
});

const connection = (
  backend: AgentBackendId,
  overrides: Partial<BackendConnectionState> = {},
): BackendConnectionState => ({
  ok: true, backend, enabled: true, installed: true, auth: 'none', ready: false,
  entry_eligible: false, application: 'stopped', ...overrides,
});

const evidence = (overrides: Partial<EntryEvidence> = {}): EntryEvidence => ({
  agentsRead: true,
  agents: [CLAUDE],
  defaultAgentName: null,
  targets: [],
  connections: [connection('claude', { ready: true, entry_eligible: true, application: 'applied' })],
  suppliesRead: true,
  supplies: [claudeSupply()],
  runtime: RUNTIME_OK,
  cliFound: { claude: true, codex: false, opencode: false },
  ...overrides,
});

const ipcDown = () => Promise.reject(new Error('engine_down: controller socket absent'));

const deps = (overrides: Partial<EntryGateDeps> = {}): EntryGateDeps => ({
  agentReads: { refresh: vi.fn(async () => ({ kind: 'current' as const, value: [claudeSupply()] })), read: vi.fn(), readValue: vi.fn(), invalidate: vi.fn() },
  getRuntimeStatus: vi.fn(async () => RUNTIME_OK),
  getBackendConnection: vi.fn(async (backend) => connection(backend, { application: 'applied', ready: true, entry_eligible: true })),
  listAgents: vi.fn(async () => ({ ok: true, agents: [CLAUDE], default_agent_name: null })),
  getVibeAgent: vi.fn(async () => ({ ok: false, agent: null as never, default_agent_name: null })),
  detectCli: vi.fn(async (binary: string) => ({ found: true, path: binary })),
  ...overrides,
});

describe('B2 stopped recovery independent of IPC', () => {
  it('retains a confirmed stopped controller recovery when IPC supply is unavailable', async () => {
    const gate = deps({
      agentReads: { refresh: vi.fn(ipcDown), read: vi.fn(), readValue: vi.fn(), invalidate: vi.fn() },
      getRuntimeStatus: vi.fn(ipcDown),
      getBackendConnection: vi.fn(async (backend) => connection(backend, {
        enabled: true, installed: true, ready: false, entry_eligible: true, application: 'stopped',
      })),
      listAgents: vi.fn(async () => ({ ok: true, agents: [], default_agent_name: null })),
    });
    const read = await readEntryEvidence(gate);
    expect(read).not.toBeNull();
    expect(read!.suppliesRead).toBe(false);
    expect(read!.runtime).toBeNull();
    expect(read!.connections).toHaveLength(SETUP_LINEUP.length);
    expect(admitEntry(read!)).toMatchObject({ kind: 'refused', startable: true });
    expect(entryCandidates(read!)).toEqual([]);
  });

  it('is startable without entry_eligible or any listed assistant', async () => {
    const gate = deps({
      agentReads: { refresh: vi.fn(ipcDown), read: vi.fn(), readValue: vi.fn(), invalidate: vi.fn() },
      getRuntimeStatus: vi.fn(ipcDown),
      getBackendConnection: vi.fn(async (backend) => connection(backend, {
        ready: false, entry_eligible: false, application: 'stopped',
      })),
      listAgents: vi.fn(async () => ({ ok: true, agents: [], default_agent_name: null })),
    });
    const read = await readEntryEvidence(gate);
    expect(admitEntry(read!)).toMatchObject({ kind: 'refused', startable: true });
  });

  it.each(['unknown', 'failed', 'draining'] as const)('does not treat %s as start permission', async (application) => {
    const gate = deps({
      agentReads: { refresh: vi.fn(ipcDown), read: vi.fn(), readValue: vi.fn(), invalidate: vi.fn() },
      getRuntimeStatus: vi.fn(ipcDown),
      getBackendConnection: vi.fn(async (backend) => connection(backend, {
        ready: false, entry_eligible: true, application,
      })),
    });
    const read = await readEntryEvidence(gate);
    expect(admitEntry(read!)).toMatchObject({ kind: 'refused', startable: false });
  });

  it('does not admit unread supply even when connections look applied', () => {
    expect(admitEntry(evidence({ suppliesRead: false, supplies: [] }))).toMatchObject({
      kind: 'refused', startable: false,
    });
    expect(admitEntry(evidence({ runtime: null }))).toMatchObject({ kind: 'refused', startable: false });
  });

  it('holds when the shared refresh is stale', async () => {
    const gate = deps({
      agentReads: { refresh: vi.fn(async () => ({ kind: 'stale' as const })), read: vi.fn(), readValue: vi.fn(), invalidate: vi.fn() },
    });
    expect(await readEntryEvidence(gate)).toBeNull();
  });
});

describe('C4 configured-path CLI evidence', () => {
  it('reads a persisted cli_path and falls back to the backend name', () => {
    expect(configuredCliPath({ agents: { claude: { cli_path: '/opt/custom/claude' } } }, 'claude')).toBe('/opt/custom/claude');
    expect(configuredCliPath({ agents: { claude: { cli_path: '  /x  ' } } }, 'claude')).toBe('/x');
    expect(configuredCliPath({ agents: { claude: { enabled: true } } }, 'claude')).toBe('claude');
    expect(configuredCliPath(undefined, 'codex')).toBe('codex');
  });

  it('detects the configured path from the already-fresh prerequisite', async () => {
    const detectCli = vi.fn(async (binary: string) => ({ found: binary === '/opt/custom/claude', path: binary }));
    const gate = deps({ detectCli });
    const read = await readEntryEvidence(gate, {
      config: { agents: { claude: { enabled: true, cli_path: '/opt/custom/claude' }, codex: { enabled: true } } },
    });
    expect(detectCli).toHaveBeenCalledWith('/opt/custom/claude');
    expect(detectCli).toHaveBeenCalledWith('codex');
    expect(detectCli).toHaveBeenCalledWith('opencode');
    expect(read!.cliFound).toEqual({ claude: true, codex: false, opencode: false });
  });

  it('does not admit when detectCli is not found even if installed and cli_present', () => {
    const read = evidence({
      connections: [connection('claude', { installed: true, ready: true, entry_eligible: true, application: 'applied' })],
      cliFound: { claude: false, codex: false, opencode: false },
    });
    expect(entryCandidates(read)).toEqual([]);
    expect(admitEntry(read)).toMatchObject({ kind: 'refused', startable: false });
  });

  it('does not admit when detectCli throws', async () => {
    const gate = deps({
      detectCli: vi.fn(async () => { throw new Error('detector unread'); }),
    });
    const read = await readEntryEvidence(gate);
    expect(read!.cliFound).toEqual({ claude: false, codex: false, opencode: false });
    expect(admitEntry(read!)).toMatchObject({ kind: 'refused', startable: false });
  });

  it('does not treat connection.installed as the detector', () => {
    const read = evidence({
      connections: [connection('claude', { installed: false, ready: true, entry_eligible: true, application: 'applied' })],
      cliFound: { claude: true, codex: false, opencode: false },
    });
    expect(entryCandidates(read)).toEqual([{ agent: CLAUDE, backend: 'claude', modelId: 'openai/gpt-5.6-sol' }]);
  });

  it('cannot release entry on a stale refresh even with a found CLI', async () => {
    const gate = deps({
      agentReads: { refresh: vi.fn(async () => ({ kind: 'stale' as const })), read: vi.fn(), readValue: vi.fn(), invalidate: vi.fn() },
      detectCli: vi.fn(async () => ({ found: true, path: '/opt/custom/claude' })),
    });
    expect(await readEntryEvidence(gate, { config: { agents: { claude: { cli_path: '/opt/custom/claude' } } } })).toBeNull();
  });
});

describe('D3 a built-in assistant completes setup on its own', () => {
  const VIBEY: VibeAgentBrief = { ...CLAUDE, id: 'vb-1', name: 'vibey', backend: 'vibey', display_name: 'vibey' };
  const vibeySupply = (overrides: Partial<AgentSupply> = {}): AgentSupply => ({
    backend: 'vibey', cli_present: false, mode: 'hub', menu_kind: 'fixed',
    named_agents: [{ name: 'vibey', effective_model_id: 'gpt-5.6-sol', supply_status: 'ok' }],
    ...overrides,
  });
  const applied = (backend: AgentBackendId) => connection(backend, { ready: true, entry_eligible: true, application: 'applied' });
  const vibeyOnly = (overrides: Partial<EntryEvidence> = {}) => evidence({
    agents: [CLAUDE, VIBEY],
    connections: [applied('vibey')],
    supplies: [vibeySupply()],
    cliFound: { claude: false, codex: false, opencode: false },
    ...overrides,
  });

  it('admits the built-in Agent with no CLI on disk', () => {
    expect(entryCandidates(vibeyOnly())).toEqual([{ agent: VIBEY, backend: 'vibey', modelId: 'gpt-5.6-sol' }]);
    expect(admitEntry(vibeyOnly())).toMatchObject({ kind: 'admitted' });
  });

  it('still needs its applied connection and a runnable route', () => {
    expect(entryCandidates(vibeyOnly({ connections: [connection('vibey', { ready: false, entry_eligible: true, application: 'applied' })] }))).toEqual([]);
    expect(entryCandidates(vibeyOnly({ supplies: [vibeySupply({ named_agents: [{ name: 'vibey', effective_model_id: null, supply_status: null }] })] }))).toEqual([]);
    expect(entryCandidates(vibeyOnly({ supplies: [vibeySupply({ named_agents: [{ name: 'vibey', effective_model_id: 'gpt-5.6-sol', supply_status: 'interrupted' }] })] }))).toEqual([]);
  });

  it('reads the built-in connection with the others and detects only CLIs', async () => {
    const getBackendConnection = vi.fn(async (backend: AgentBackendId) => applied(backend));
    const detectCli = vi.fn(async (binary: string) => ({ found: true, path: binary }));
    await readEntryEvidence(deps({ getBackendConnection, detectCli }));
    expect(getBackendConnection.mock.calls.map(([backend]) => backend).sort()).toEqual([...SETUP_LINEUP].sort());
    expect(detectCli.mock.calls.map(([binary]) => binary)).not.toContain('vibey');
  });

  it('ranks the built-in assistant last, so a runnable CLI assistant stays the default', () => {
    const both = evidence({
      agents: [CLAUDE, VIBEY],
      connections: [applied('claude'), applied('vibey')],
      supplies: [claudeSupply(), vibeySupply()],
      targets: [VIBEY, CLAUDE],
    });
    expect(entryCandidates(both).map((candidate) => candidate.backend)).toEqual(['claude', 'vibey']);
    expect(chooseEntryDefault(entryCandidates(both), both)?.agent.name).toBe('claude-agent');
    const alone = vibeyOnly({ targets: [VIBEY], defaultAgentName: 'claude-agent' });
    expect(chooseEntryDefault(entryCandidates(alone), alone)?.agent.name).toBe('vibey');
  });

  it('keeps a saved built-in default that can run', () => {
    const saved = evidence({
      agents: [CLAUDE, VIBEY],
      connections: [applied('claude'), applied('vibey')],
      supplies: [claudeSupply(), vibeySupply()],
      defaultAgentName: 'vibey',
    });
    expect(chooseEntryDefault(entryCandidates(saved), saved)).toBeNull();
  });
});
