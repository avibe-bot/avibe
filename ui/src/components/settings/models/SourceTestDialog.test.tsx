// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
import type { SourceMutationSettlement, TrackSourceMutation } from './mutationSettlement';
import { SourceTestDialog } from './SourceTestDialog';
import type { Source, SourceProbeResult } from './types';

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
const settlement = {
  unread: vi.fn().mockResolvedValue({ verdict: 'degraded', reads: null, affectedChains: [] }),
  gone: vi.fn().mockResolvedValue({ verdict: 'degraded', reads: null, affectedChains: [] }),
} as unknown as SourceMutationSettlement;
const track: TrackSourceMutation = (work) => work(source, settlement);
const view = (current = source) => <I18nextProvider i18n={i18n}>
  <SourceTestDialog source={current} onClose={vi.fn()} trackMutation={track} />
</I18nextProvider>;

beforeEach(async () => {
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
  });

  it('lets an empty inventory exit to the existing manual model editor', () => {
    render(view({ ...source, models: [] }));
    expect(screen.getByText(/add a model ID manually/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Run test' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getAllByRole('button', { name: 'Close' }).length).toBeGreaterThan(0);
  });
});
