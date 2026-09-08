// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
import type { SourceMutationLanding, SourceMutationSettlement, TrackSourceMutation } from './mutationSettlement';
import { readyRegion, unreadRegion } from './regionRead';
import { SourceTestDialog } from './SourceTestDialog';
import { CONTRACT_VERSION, type Source, type SourceProbeResult } from './types';

const source: Source = {
  id: 'src_test0001', kind: 'api_key', vendor: 'custom', display_name: 'My relay',
  protocol: 'openai_chat', base_url: 'https://relay.example/v1', supply_channel: 'hub',
  billing: 'metered', state: { status: 'standby', retry_at: null, detail_key: null },
  credential_ref: 'cred_test001', verification_pending: 'vp_original', last_discovered_at: null,
  models: ['first-model', 'gpt-5.6-luna', 'manual-model'].map((id) => ({
    id, display_name: null, origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null,
  })),
};
const answer = (patch: Partial<SourceProbeResult> = {}): SourceProbeResult => ({
  source_id: source.id, model_id: 'gpt-5.6-luna', protocol: source.protocol,
  reachable: true, latency_ms: 123, error: null, ...patch,
});
const landing = (sources: Source[] = [source]): SourceMutationLanding => ({
  verdict: 'landed', affectedChains: [], reads: {
    sources: readyRegion(sources), supply: readyRegion([]), chains: readyRegion({}),
    runtime: readyRegion({
      contract_version: CONTRACT_VERSION,
      manifest: { name: 'cliproxyapi', resolution: 'unresolved', assets: [] },
      status: { verified: false, health: 'not_started' },
    }),
  },
});
const settlement = {
  unread: vi.fn(),
  gone: vi.fn().mockResolvedValue({ verdict: 'degraded', reads: null, affectedChains: [] }),
} as unknown as SourceMutationSettlement;
const track: TrackSourceMutation = (work) => work(source, settlement);
const view = (current = source) => <I18nextProvider i18n={i18n}>
  <SourceTestDialog source={current} onClose={vi.fn()} trackMutation={track} />
</I18nextProvider>;

beforeEach(async () => {
  vi.mocked(settlement.unread).mockReset().mockResolvedValue(landing());
  await i18n.changeLanguage('en');
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('SourceTestDialog', () => {
  it('does no work until the user starts and invokes only the chosen source/model', async () => {
    const probe = vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer({ model_id: 'manual-model' }));
    const refresh = vi.spyOn(modelsApi, 'refreshSource');
    render(view());
    expect(screen.getByRole('combobox').textContent).toContain('gpt-5.6-luna');
    expect(probe).not.toHaveBeenCalled();
    const user = userEvent.setup();
    await user.click(screen.getByRole('combobox'));
    await user.click(await screen.findByRole('option', { name: 'manual-model' }));
    await user.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByText(/manual-model responded successfully/);
    expect(probe).toHaveBeenCalledExactlyOnceWith(source.id, 'manual-model');
    expect(refresh).not.toHaveBeenCalled();
  });

  it('keeps the result when success clears pending verification', async () => {
    vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer());
    const rendered = render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByText(/responded successfully/);
    rendered.rerender(view({ ...source, verification_pending: null }));
    expect(screen.getByText(/responded successfully/)).toBeTruthy();
  });

  it('cannot show a late success for a replacement credential', async () => {
    let resolve!: (result: SourceProbeResult) => void;
    vi.spyOn(modelsApi, 'probeSource').mockReturnValue(new Promise((done) => { resolve = done; }));
    const rendered = render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    rendered.rerender(view({ ...source, credential_ref: 'cred_new', verification_pending: 'vp_new' }));
    resolve(answer());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run test' }).hasAttribute('disabled')).toBe(false));
    expect(screen.queryByText(/responded successfully/)).toBeNull();
  });

  it('explains unavailable results without losing the selected model or provider', async () => {
    vi.spyOn(modelsApi, 'probeSource').mockRejectedValue(new Error('unavailable'));
    render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Your provider is still saved');
    expect(screen.getByRole('combobox').textContent).toContain('gpt-5.6-luna');
    expect(settlement.unread).toHaveBeenCalledTimes(1);
  });

  it.each([true, false])('publishes no verdict before reconciliation completes: reachable=%s', async (reachable) => {
    vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer({ reachable }));
    let resolve!: (value: SourceMutationLanding) => void;
    vi.mocked(settlement.unread).mockReturnValue(new Promise((done) => { resolve = done; }));
    render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await waitFor(() => expect(settlement.unread).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    resolve(landing());
    expect((await screen.findByRole('status')).textContent).toContain('gpt-5.6-luna');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it.each([true, false])('does not retry or publish a verdict after rejected reconciliation: reachable=%s', async (reachable) => {
    const probe = vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer({ reachable }));
    vi.mocked(settlement.unread).mockRejectedValue(new Error('Source list unavailable'));
    render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('status')).toBeNull();
    expect(settlement.unread).toHaveBeenCalledTimes(1);
    expect(probe).toHaveBeenCalledTimes(1);
  });

  it.each(['unread', 'stale', 'deleted', 'credential', 'marker', 'base_url', 'protocol', 'model'])('requires a current matching Source after reconciliation: %s', async (change) => {
    vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer());
    const current = { ...source };
    if (change === 'credential') current.credential_ref = 'cred_replacement';
    if (change === 'marker') current.verification_pending = 'vp_replacement';
    if (change === 'base_url') current.base_url = 'https://other.example/v1';
    if (change === 'protocol') current.protocol = 'anthropic';
    if (change === 'model') current.models = [];
    const result = landing(change === 'deleted' ? [] : [current]);
    if (change === 'unread') result.reads!.sources = unreadRegion();
    if (change === 'stale') result.verdict = 'degraded';
    vi.mocked(settlement.unread).mockResolvedValue(result);
    render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('status')).toBeNull();
    expect(settlement.unread).toHaveBeenCalledTimes(1);
  });

  it('accepts reconciliation that clears the tested credential marker', async () => {
    vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer());
    vi.mocked(settlement.unread).mockResolvedValue(landing([{ ...source, verification_pending: null }]));
    render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByText(/responded successfully/);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('invalidates a reconciliation result when the dialog identity changes while it waits', async () => {
    vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer());
    let resolve!: (value: SourceMutationLanding) => void;
    vi.mocked(settlement.unread).mockReturnValue(new Promise((done) => { resolve = done; }));
    const rendered = render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await waitFor(() => expect(settlement.unread).toHaveBeenCalledTimes(1));
    rendered.rerender(view({ ...source, credential_ref: 'cred_new', verification_pending: 'vp_new' }));
    resolve(landing());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run test' }).hasAttribute('disabled')).toBe(false));
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('replaces an unconfirmed status only after a new explicit test settles', async () => {
    const probe = vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer());
    vi.mocked(settlement.unread).mockRejectedValueOnce(new Error('unavailable'));
    render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByRole('alert');
    expect(probe).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByText(/responded successfully/);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(probe).toHaveBeenCalledTimes(2);
    expect(settlement.unread).toHaveBeenCalledTimes(2);
  });

  it.each(['remove', 'retire', 'empty'])('clears a completed result when inventory changes selection: %s', async (change) => {
    vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer());
    const rendered = render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByText(/responded successfully/);
    const models = change === 'empty' ? [] : change === 'remove'
      ? source.models.filter((model) => model.id !== 'gpt-5.6-luna')
      : source.models.map((model) => ({ ...model, retired: model.id === 'gpt-5.6-luna' }));
    rendered.rerender(view({ ...source, models }));
    expect(screen.queryByRole('status')).toBeNull();
    if (change !== 'empty') expect(screen.getByRole('combobox').textContent).toContain('first-model');
  });

  it('rejects a late result after inventory chooses a different model', async () => {
    let resolve!: (result: SourceProbeResult) => void;
    vi.spyOn(modelsApi, 'probeSource').mockReturnValue(new Promise((done) => { resolve = done; }));
    const rendered = render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    rendered.rerender(view({ ...source, models: source.models.filter((model) => model.id !== 'gpt-5.6-luna') }));
    resolve(answer());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run test' }).hasAttribute('disabled')).toBe(false));
    expect(screen.getByRole('combobox').textContent).toContain('first-model');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('preserves a result across inventory updates that retain the selected model', async () => {
    vi.spyOn(modelsApi, 'probeSource').mockResolvedValue(answer());
    const rendered = render(view());
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findByText(/responded successfully/);
    rendered.rerender(view({ ...source, models: [...source.models].reverse() }));
    expect(screen.getByRole('combobox').textContent).toContain('gpt-5.6-luna');
    expect(screen.getByText(/responded successfully/)).toBeTruthy();
  });

  it('lets an empty inventory exit to the existing manual model editor', () => {
    render(view({ ...source, models: [] }));
    expect(screen.getByText(/add a model ID manually/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Run test' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getAllByRole('button', { name: 'Close' }).length).toBeGreaterThan(0);
  });
});
