// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { RouteChainDialog } from './RouteChainDialog';
import { ApiCallError, modelsApi } from './modelsApi';
import { readyRegion } from './regionRead';
import type { AgentChain, AgentSupply, Source } from './types';

const sources: Source[] = ['a', 'b'].map((id) => ({
  id: `src_${id}`, display_name: `Provider ${id}`, kind: 'api_key',
  vendor: 'openai', protocol: 'openai_responses', supply_channel: 'hub',
  billing: 'metered', last_discovered_at: null,
  state: { status: 'active', retry_at: null, detail_key: null }, models: [],
}));
const hops = sources.map((source) => ({ source_id: source.id, model_id: 'gpt-test' }));
const makeChain = (backend: AgentSupply['backend'], routeOrigin: 'automatic' | 'passthrough' | 'manual'): AgentChain => ({
  contract_version: 10, backend, model_id: 'gpt-test', route_origin: routeOrigin,
  manual_override: routeOrigin === 'manual' ? { hops } : null,
  current: hops[0], supply_state: 'ok',
  chain: hops.map((hop) => ({ ...hop, channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null })),
});
const mount = (chain: AgentChain) => {
  const close = vi.fn();
  vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(chain);
  const agent: AgentSupply = {
    backend: chain.backend, cli_present: true, mode: 'hub', menu_kind: 'fixed',
    sources: { order: sources.map((source) => source.id), eligibility: sources.map((source) => ({ source_id: source.id, eligible: true })) },
  };
  render(<I18nextProvider i18n={i18n}><RouteChainDialog
    selection={{ agent, modelId: chain.model_id, read: readyRegion(chain) }}
    sources={sources} onClose={close} readAgents={vi.fn()} readSources={vi.fn()}
  /></I18nextProvider>);
  return close;
};
const footer = () => within(document.querySelector<HTMLElement>('.model-hub-route-foot')!);

beforeEach(async () => {
  await i18n.changeLanguage('en');
  vi.spyOn(modelsApi, 'getAgentProvenance').mockResolvedValue(null);
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('direct route editing', () => {
  it.each(['claude', 'codex', 'opencode'] as const)('%s opens every origin directly editable without changing intent', async (backend) => {
    for (const origin of ['automatic', 'passthrough', 'manual'] as const) {
      const put = vi.spyOn(modelsApi, 'putAgentChain');
      const restore = vi.spyOn(modelsApi, 'restoreAgentChain');
      mount(makeChain(backend, origin));
      expect(await screen.findAllByRole('button', { name: 'Edit hop' })).toHaveLength(2);
      expect(screen.queryByRole('button', { name: 'Edit route' })).toBeNull();
      expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', true);
      expect(put).not.toHaveBeenCalled();
      expect(restore).not.toHaveBeenCalled();
      cleanup();
      vi.restoreAllMocks();
      vi.spyOn(modelsApi, 'getAgentProvenance').mockResolvedValue(null);
    }
  });

  it('cancels a changed inherited draft in place and observes current saved authority', async () => {
    const user = userEvent.setup();
    const chain = makeChain('codex', 'automatic');
    const close = mount(chain);
    const put = vi.spyOn(modelsApi, 'putAgentChain');
    await user.click((await screen.findAllByRole('button', { name: 'Remove hop' }))[0]);
    expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', false);
    const latest = { ...chain, chain: [chain.chain[0]] };
    vi.mocked(modelsApi.getAgentChain).mockResolvedValue(latest);
    await user.click(footer().getByRole('button', { name: 'Cancel changes' }));
    await waitFor(() => expect(modelsApi.getAgentChain).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Provider a')).toBeTruthy();
    expect(screen.queryByText('Provider b')).toBeNull();
    expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', true);
    expect(close).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
  });

  it('pins identical inherited hops only through explicit Pin and Save', async () => {
    const user = userEvent.setup();
    const chain = makeChain('codex', 'automatic');
    mount(chain);
    const put = vi.spyOn(modelsApi, 'putAgentChain').mockResolvedValue({
      chain: makeChain('codex', 'manual'), removed_hops: [], interrupted: [],
    });
    await user.click(await screen.findByRole('button', { name: 'Pin current route' }));
    expect(put).not.toHaveBeenCalled();
    await user.click(footer().getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(put).toHaveBeenCalledWith('codex', 'gpt-test', { hops }));
  });

  it('does not pin from a dismissed picker, identical candidate or unmoved keyboard grab', async () => {
    const user = userEvent.setup();
    const close = mount(makeChain('codex', 'passthrough'));
    const put = vi.spyOn(modelsApi, 'putAgentChain');
    const edit = (await screen.findAllByRole('button', { name: 'Edit hop' }))[0];
    await user.click(edit);
    await user.keyboard('{Escape}');
    expect(close).not.toHaveBeenCalled();
    await user.click(edit);
    await user.type(screen.getByLabelText('Exact model ID'), 'gpt-test');
    expect(screen.getByRole('button', { name: 'Replace' })).toHaveProperty('disabled', true);
    await user.click(screen.getByRole('button', { name: 'Replace' }));
    await user.keyboard('{Escape}');
    const grip = (await screen.findAllByRole('button', { name: 'Reorder this hop' }))[0];
    grip.focus();
    await user.keyboard('{Space}{Space}');
    expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', true);
    expect(footer().queryByRole('button', { name: 'Cancel changes' })).toBeNull();
    expect(put).not.toHaveBeenCalled();
  });

  it.each(['automatic', 'restore'] as const)('Escape restores %s intent after moving a grabbed row', async (origin) => {
    const user = userEvent.setup();
    mount(makeChain('codex', origin === 'restore' ? 'manual' : 'automatic'));
    await screen.findAllByRole('button', { name: 'Edit hop' });
    if (origin === 'restore') {
      vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(makeChain('codex', 'automatic'));
      await user.click(screen.getByRole('button', { name: 'Restore automatic' }));
      await screen.findByRole('button', { name: 'Undo restore' });
    }
    screen.getAllByRole('button', { name: 'Reorder this hop' })[0].focus();
    await user.keyboard('{Space}{ArrowDown}');
    expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', false);
    await user.keyboard('{Escape}');
    expect([...document.querySelectorAll('.model-hub-route-hop-name')].map((row) => row.textContent))
      .toEqual(['Provider a', 'Provider b']);
    if (origin === 'restore') {
      expect(screen.getByRole('button', { name: 'Undo restore' })).toBeTruthy();
      expect(document.querySelector('.model-hub-route-preview')).not.toBeNull();
      await user.click(screen.getByRole('button', { name: 'Undo restore' }));
    } else {
      expect(screen.getByRole('button', { name: 'Pin current route' })).toBeTruthy();
    }
    expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', true);
  });

  it('edits an inherited restore preview into a new manual draft and saves only that draft', async () => {
    const user = userEvent.setup();
    mount(makeChain('codex', 'manual'));
    vi.spyOn(modelsApi, 'previewAgentChain').mockResolvedValue(makeChain('codex', 'automatic'));
    const restore = vi.spyOn(modelsApi, 'restoreAgentChain');
    const put = vi.spyOn(modelsApi, 'putAgentChain').mockResolvedValue({
      chain: makeChain('codex', 'manual'), removed_hops: [], interrupted: [],
    });
    await user.click(await screen.findByRole('button', { name: 'Restore automatic' }));
    await screen.findByRole('button', { name: 'Undo restore' });
    await user.click(screen.getAllByRole('button', { name: 'Remove hop' })[0]);
    expect(screen.queryByRole('button', { name: 'Undo restore' })).toBeNull();
    expect(document.querySelector('.model-hub-route-preview')).toBeNull();
    await user.click(footer().getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(put).toHaveBeenCalledWith('codex', 'gpt-test', { hops: [hops[1]] }));
    expect(restore).not.toHaveBeenCalled();
  });

  it('invalidates a pending restore on Cancel changes, without closing or applying the late preview', async () => {
    const user = userEvent.setup();
    let resolve!: (chain: AgentChain) => void;
    vi.spyOn(modelsApi, 'previewAgentChain').mockReturnValue(new Promise((accept) => { resolve = accept; }));
    const close = mount(makeChain('codex', 'manual'));
    await user.click(await screen.findByRole('button', { name: 'Restore automatic' }));
    await user.click(footer().getByRole('button', { name: 'Cancel changes' }));
    await screen.findAllByRole('button', { name: 'Edit hop' });
    await act(async () => resolve({ ...makeChain('codex', 'automatic'), chain: [] }));
    expect(screen.getAllByRole('button', { name: 'Edit hop' })).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Undo restore' })).toBeNull();
    expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', true);
    expect(close).not.toHaveBeenCalled();
  });

  it('shows an unread state if the fresh read on Cancel changes fails, then retries the read only', async () => {
    const user = userEvent.setup();
    const close = mount(makeChain('codex', 'automatic'));
    const put = vi.spyOn(modelsApi, 'putAgentChain');
    await user.click(await screen.findByRole('button', { name: 'Pin current route' }));
    vi.mocked(modelsApi.getAgentChain).mockRejectedValueOnce(new Error('offline'));
    await user.click(footer().getByRole('button', { name: 'Cancel changes' }));
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Edit hop' })).toBeNull();
    expect(footer().getByRole('button', { name: 'Save' })).toHaveProperty('disabled', true);
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('button', { name: 'Pin current route' });
    expect(modelsApi.getAgentChain).toHaveBeenCalledTimes(3);
    expect(put).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });

  it.each(['rejected', 'unknown'] as const)('offers local cancellation only for a known rejection, not an %s outcome', async (outcome) => {
    const user = userEvent.setup();
    const close = mount(makeChain('codex', 'automatic'));
    const put = vi.spyOn(modelsApi, 'putAgentChain').mockRejectedValue(outcome === 'rejected'
      ? new ApiCallError('invalid_route')
      : new Error('response lost'));
    await user.click(await screen.findByRole('button', { name: 'Pin current route' }));
    await user.click(footer().getByRole('button', { name: 'Save' }));
    await screen.findByRole('button', { name: 'Retry' });
    if (outcome === 'rejected') {
      await user.click(footer().getByRole('button', { name: 'Cancel changes' }));
      await screen.findByRole('button', { name: 'Pin current route' });
      expect(close).not.toHaveBeenCalled();
      expect(modelsApi.getAgentChain).toHaveBeenCalledTimes(2);
    } else {
      expect(footer().queryByRole('button', { name: 'Cancel changes' })).toBeNull();
      await user.click(footer().getByRole('button', { name: 'Close' }));
      expect(close).toHaveBeenCalledTimes(1);
      expect(modelsApi.getAgentChain).toHaveBeenCalledTimes(1);
    }
    expect(put).toHaveBeenCalledTimes(1);
  });
});
