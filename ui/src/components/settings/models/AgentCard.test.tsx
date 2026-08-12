// @vitest-environment jsdom
import type { ComponentProps } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

import i18n from '@/i18n';
import { AgentCard as RuntimeAgentCard } from './AgentCard';
import { modelChainKey, NOMINAL_MODEL_BASELINE } from './modelRows';
import { readyRegion } from './regionRead';
import { freshRuntimeProjection } from './runtimeLifecycle';
import type { AgentSupply, RuntimeDependency, Source } from './types';

const runtime: RuntimeDependency = {
  contract_version: 5,
  manifest: { name: 'cliproxyapi', version: '1.0.0', source_sha: 'fixture', assets: [] },
  status: { installed_version: '1.0.0', verified: true, listening: null, health: 'ok', last_check: null },
};
const AgentCard = (props: Omit<ComponentProps<typeof RuntimeAgentCard>, 'runtime'>) =>
  <RuntimeAgentCard {...props} runtime={freshRuntimeProjection(readyRegion(runtime))} />;

const source = (id: string, name: string): Source => ({
  id, last_discovered_at: null, kind: 'api_key', vendor: 'anthropic', display_name: name,
  protocol: 'anthropic', supply_channel: 'hub', billing: 'metered', state: { status: 'active', retry_at: null, detail_key: null },
  models: [{ id: 'claude-opus-4-6', origin: 'discovered', reasoning_efforts: [] }],
});
const hubAgent: AgentSupply = {
  backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed', selected_model_id: 'claude-opus-4-6', selected_model_explicit: true,
  sources: { order: ['src_a', 'src_b'], eligibility: [{ source_id: 'src_a', eligible: true }, { source_id: 'src_b', eligible: true }] },
  routes: { 'claude-opus-4-6': { hops: [{ source_id: 'src_a', model_id: 'claude-opus-4-6' }, { source_id: 'src_b', model_id: 'claude-opus-4-6' }] } },
  supply_status: 'degraded', model_supply: [{ model_id: 'claude-opus-4-6', chain_length: 2, has_runnable_hop: true }], named_agents: [], builtin_models: ['claude-opus-4-6'], menu: null,
};

afterEach(cleanup);

describe('AgentCard', () => {
  it('derives takeover from the exact current hop', () => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ contract_version: 5, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(/Backup/)).toBeTruthy();
    expect(screen.getByText(/takeover/i)).toBeTruthy();
  });

  it('does not call a later current hop takeover unless the head is unavailable for cooldown', () => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ contract_version: 5, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'native_cli', health: 'healthy', runnable: false, reason: 'native_cli_unavailable', retry_at: null }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(/Backup/)).toBeTruthy();
    expect(screen.queryByText(/takeover/i)).toBeNull();
  });

  it('hides current and takeover projections while the runtime is stopped', () => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    const stopped = { ...runtime, status: { ...runtime.status, health: 'down' as const } };
    render(<I18nextProvider i18n={i18n}><RuntimeAgentCard runtime={freshRuntimeProjection(readyRegion(stopped))} agents={[hubAgent]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ contract_version: 5, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.queryByText(/Backup/)).toBeNull();
    expect(screen.queryByText(/takeover/i)).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('offers only gateway enablement and ignores stale chain data in direct mode', () => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[{ ...hubAgent, mode: 'direct', sources: null, routes: null, supply_status: null, model_supply: null }]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ contract_version: 5, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);
    expect(screen.queryByText(/Source order/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /route chain/i })).toBeNull();
    expect(screen.queryByText(/Backup/)).toBeNull();
    expect(screen.queryByText(/takeover/i)).toBeNull();
  });

  it('renders the AgentSupply collapse projection and rereads chains on expand and collapse', async () => {
    const onProbeSettled = vi.fn();
    const models = Array.from({ length: NOMINAL_MODEL_BASELINE + 2 }, (_, index) => `model-${index + 1}`);
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[{
      ...hubAgent,
      builtin_models: models,
      model_supply: models.map((modelId) => ({ model_id: modelId, chain_length: 1, has_runnable_hop: true })),
      routes: {},
    }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={onProbeSettled} /></I18nextProvider>);

    expect(screen.queryByText(models.at(-1) as string)).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /more model/i }));
    expect(screen.getByText(models.at(-1) as string)).toBeTruthy();
    expect(onProbeSettled).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole('button', { name: /more model/i }));
    expect(screen.queryByText(models.at(-1) as string)).toBeNull();
    expect(onProbeSettled).toHaveBeenCalledTimes(2);
    expect(onProbeSettled).toHaveBeenNthCalledWith(1, expect.objectContaining({ backend: 'claude' }));
    expect(onProbeSettled).toHaveBeenNthCalledWith(2, expect.objectContaining({ backend: 'claude' }));
  });

  it('keeps a nonempty paused route visible beyond the nominal collapsed rows', () => {
    const models = Array.from({ length: NOMINAL_MODEL_BASELINE + 2 }, (_, index) => `model-${index + 1}`);
    const pausedModel = models.at(-1) as string;
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[{
      ...hubAgent,
      builtin_models: models,
      routes: {},
      model_supply: models.map((modelId) => ({
        model_id: modelId,
        chain_length: 1,
        has_runnable_hop: modelId !== pausedModel,
      })),
    }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText(pausedModel)).toBeTruthy();
    expect(screen.getByText(/^Supply paused$|^供给已暂停$/i)).toBeTruthy();
  });

  it('keeps a chain reread reachable when a short group is unresolved', async () => {
    const onProbeSettled = vi.fn();
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[]} chains={{ [key]: { kind: 'unread', retryable: true } }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={onProbeSettled} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    expect(onProbeSettled).toHaveBeenCalledOnce();
    expect(onProbeSettled).toHaveBeenCalledWith(expect.objectContaining({ backend: 'claude' }));
  });

  it('opens Frame 02 with the exact backend and model context', async () => {
    const onOpenRoute = vi.fn();
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={onOpenRoute} onProbeSettled={vi.fn()} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /Open claude-opus-4-6 route chain/i }));

    expect(onOpenRoute).toHaveBeenCalledWith(hubAgent, 'claude-opus-4-6', expect.any(HTMLElement));
  });

  it('replaces the gateway status slot and action with an in-place retry after leaving fails', async () => {
    const onSwitchDirect = vi.fn();
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set(['claude'])} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={onSwitchDirect} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText(/did not go through/i)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(onSwitchDirect).toHaveBeenCalledWith(hubAgent);
  });
});
