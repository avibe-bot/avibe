// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { createSourceCollectionReadAuthority } from './collectionReadAuthority';
import { ApiCallError, modelsApi } from './modelsApi';
import { SourceOrderDrawer } from './SourceOrderDrawer';
import type { AgentSupply, Source } from './types';

const source = (id: string, displayName: string): Source => ({
  id,
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: displayName,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
});

const sources = [source('src_a', 'Primary'), source('src_b', 'Backup')];
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
const agent: AgentSupply = {
  backend: 'claude',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  sources: {
    order: ['src_a', 'src_b'],
    eligibility: sources.map((item) => ({ source_id: item.id, eligible: true })),
  },
};

const renderDrawer = (overrides: Partial<React.ComponentProps<typeof SourceOrderDrawer>> = {}) => render(
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <SourceOrderDrawer
        open
        agent={agent}
        sources={sources}
        sourceReads={createSourceCollectionReadAuthority(modelsApi)}
        onClose={vi.fn()}
        onSaved={vi.fn()}
        orderWrite={{ pending: false, track: async (work) => work() }}
        {...overrides}
      />
    </ToastProvider>
  </I18nextProvider>,
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  vi.spyOn(modelsApi, 'listSources').mockResolvedValue(sources);
  vi.spyOn(modelsApi, 'putAgentSources').mockResolvedValue(agent);
});

describe('SourceOrderDrawer keyboard ordering', () => {
  it('echoes the exact effective-removal guard before saving default membership', async () => {
    const user = userEvent.setup();
    const hops = [{ backend: 'claude' as const, menu_model: 'model-a', position: 1, source_id: 'src_a', model_id: 'model-a' }];
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    const put = vi.spyOn(modelsApi, 'putAgentSources')
      .mockRejectedValueOnce(new ApiCallError('source_in_route_chain', undefined, true, [], [], hops))
      .mockResolvedValueOnce({ ...agent, sources: { ...agent.sources!, order: ['src_b'] } });
    renderDrawer();
    await user.click((await screen.findAllByRole('button', { name: 'Remove from order' }))[0]);
    await user.click(screen.getByRole('button', { name: 'Save default routing' }));
    await screen.findByText('Review affected routes');
    expect(put).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole('button', { name: 'Save default routing' }));
    await waitFor(() => expect(put).toHaveBeenLastCalledWith('claude', { order: ['src_b'], force: true, would_remove_hops: hops, would_interrupt: [] }));
  });
  it('reconciles an unknown order write without resubmitting the mutation', async () => {
    const user = userEvent.setup();
    const committed = { ...agent, sources: { ...agent.sources!, order: ['src_b'] } };
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValueOnce(agent).mockResolvedValueOnce(committed);
    const put = vi.spyOn(modelsApi, 'putAgentSources').mockRejectedValueOnce(new TypeError('response lost'));
    const onSaved = vi.fn();
    renderDrawer({ onSaved });
    await user.click((await screen.findAllByRole('button', { name: 'Remove from order' }))[0]);
    await user.click(screen.getByRole('button', { name: 'Save default routing' }));
    await user.click(await screen.findByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(committed));
    expect(put).toHaveBeenCalledTimes(1);
  });
  it('grabs, moves, announces, and restores the pre-grab order on Escape', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    renderDrawer();
    const handles = await screen.findAllByRole('button', { name: 'Reorder source' });
    expect(screen.getByRole('heading', { name: 'Claude Code · Default routing' })).toBeTruthy();
    expect(screen.getByText('Applies to automatic and passthrough routes. Saved manual routes stay unchanged.')).toBeTruthy();
    expect(screen.getByText('Available for manual routes.')).toBeTruthy();
    expect(handles[0].className).toContain('cursor-grab');

    handles[0].focus();
    await user.keyboard('[Space]');
    expect(handles[0].getAttribute('aria-grabbed')).toBe('true');

    await user.keyboard('[ArrowDown]');
    expect(screen.getByText('Moved Primary to position 2 of 2.')).toBeTruthy();
    expect(document.activeElement).toBe(screen.getAllByRole('button', { name: 'Reorder source' })[1]);

    await user.keyboard('[Escape]');
    const restored = screen.getAllByRole('button', { name: 'Reorder source' });
    expect(restored[0].getAttribute('aria-grabbed')).toBe('false');
    expect(restored[0].closest('li')?.textContent).toContain('Primary');
  });

  it('moves focus without moving the order when the row is not grabbed', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    renderDrawer();
    const handles = await screen.findAllByRole('button', { name: 'Reorder source' });

    handles[0].focus();
    await user.keyboard('[ArrowDown]');

    expect(document.activeElement?.closest('li')?.textContent).toContain('Backup');
    expect(handles[0].closest('li')?.textContent).toContain('Primary');
  });

  it('reads the current order on open and keeps the moved draft after a failed save', async () => {
    const user = userEvent.setup();
    const getAgentSources = vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    const putAgentSources = vi.spyOn(modelsApi, 'putAgentSources').mockRejectedValue(new Error('offline'));
    renderDrawer();

    await user.click((await screen.findAllByRole('button', { name: 'Remove from order' }))[0]);
    await user.click(screen.getByRole('button', { name: 'Remove from order' }));
    await user.click(screen.getByRole('button', { name: 'Save default routing' }));

    await screen.findByText('Could not confirm the saved default routing. Retry to check.');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.getByText('This order is empty. Add a source from below.')).toBeTruthy();
    expect(getAgentSources).toHaveBeenCalledWith('claude');
    expect(putAgentSources).toHaveBeenCalledWith('claude', { order: [] });
  });

  it('saves only default source membership through the source endpoint', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    const putAgentSources = vi.spyOn(modelsApi, 'putAgentSources').mockResolvedValue(agent);
    const reorder = vi.spyOn(modelsApi, 'reorderAgentChains');
    renderDrawer();

    await user.click((await screen.findAllByRole('button', { name: 'Remove from order' }))[0]);
    await user.click(screen.getByRole('button', { name: 'Save default routing' }));

    await waitFor(() => expect(putAgentSources).toHaveBeenCalledWith('claude', { order: ['src_b'] }));
    expect(reorder).not.toHaveBeenCalled();
  });

  it('does not rewrite routes when default membership and order are unchanged', async () => {
    const user = userEvent.setup();
    const savedAgent: AgentSupply = {
      ...agent,
      sources: {
        order: ['src_b', 'src_a'],
        eligibility: sources.map((item) => ({ source_id: item.id, eligible: true })),
      },
    };
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(savedAgent);
    const putAgentSources = vi.spyOn(modelsApi, 'putAgentSources').mockResolvedValue(savedAgent);
    renderDrawer({ agent: savedAgent });

    const save = await screen.findByRole('button', { name: 'Save default routing' });
    expect(save.hasAttribute('disabled')).toBe(true);
    await user.click(save);

    expect(putAgentSources).not.toHaveBeenCalled();
  });

  it('keeps a read failure inside the drawer and retries the collection read', async () => {
    const user = userEvent.setup();
    const read = vi.spyOn(modelsApi, 'getAgentSources')
      .mockRejectedValueOnce(new Error('unread'))
      .mockResolvedValueOnce(agent);
    renderDrawer();

    await screen.findByText('The source list could not be read');
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Reorder source' })).toHaveLength(2));
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('disables saving when no source is eligible', async () => {
    const noEligible: AgentSupply = {
      ...agent,
      sources: {
        order: [],
        eligibility: sources.map((item) => ({ source_id: item.id, eligible: false })),
      },
    };
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(noEligible);
    renderDrawer({ agent: noEligible });

    await screen.findByText('No source is available to this backend yet.');
    expect(screen.getByRole('button', { name: 'Save default routing' }).hasAttribute('disabled')).toBe(true);
  });

  it('uses the source inventory read in the same generation as the Agent order', async () => {
    const newest = source('src_c', 'Newest source');
    vi.mocked(modelsApi.listSources).mockResolvedValueOnce([...sources, newest]);
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue({
      ...agent,
      sources: {
        order: ['src_a', 'src_b', 'src_c'],
        eligibility: [...(agent.sources?.eligibility ?? []), { source_id: newest.id, eligible: true }],
      },
    });

    renderDrawer();

    expect(await screen.findByText('Newest source')).toBeTruthy();
    expect(screen.getAllByRole('button', { name: 'Reorder source' })).toHaveLength(3);
  });

  it('renders a composition hole and performs exactly one regroup read', async () => {
    const newest = source('src_c', 'Newest source');
    const nextAgent = {
      ...agent,
      sources: {
        order: ['src_a', 'src_b', 'src_c'],
        eligibility: [...(agent.sources?.eligibility ?? []), { source_id: newest.id, eligible: true }],
      },
    };
    const regroupedAgent = deferred<AgentSupply>();
    const regroupedSources = deferred<Source[]>();
    const agentRead = vi.spyOn(modelsApi, 'getAgentSources')
      .mockResolvedValueOnce(nextAgent)
      .mockReturnValueOnce(regroupedAgent.promise);
    vi.mocked(modelsApi.listSources)
      .mockResolvedValueOnce(sources)
      .mockReturnValueOnce(regroupedSources.promise);

    renderDrawer();

    expect(await screen.findByText('src_c')).toBeTruthy();
    expect(agentRead).toHaveBeenCalledTimes(2);
    await act(async () => {
      regroupedAgent.resolve(nextAgent);
      regroupedSources.resolve([...sources, newest]);
      await Promise.all([regroupedAgent.promise, regroupedSources.promise]);
    });
    expect(await screen.findByText('Newest source')).toBeTruthy();
    expect(agentRead).toHaveBeenCalledTimes(2);
  });

  it('regroups when the Source inventory is newer than the Agent projection', async () => {
    const newest = source('src_c', 'Newest source');
    const regroupedAgent = deferred<AgentSupply>();
    const regroupedSources = deferred<Source[]>();
    const nextAgent = {
      ...agent,
      sources: {
        order: ['src_a', 'src_b'],
        eligibility: [...(agent.sources?.eligibility ?? []), { source_id: newest.id, eligible: true }],
      },
    };
    const agentRead = vi.spyOn(modelsApi, 'getAgentSources')
      .mockResolvedValueOnce(agent)
      .mockReturnValueOnce(regroupedAgent.promise);
    vi.mocked(modelsApi.listSources)
      .mockResolvedValueOnce([...sources, newest])
      .mockReturnValueOnce(regroupedSources.promise);

    renderDrawer();

    await waitFor(() => expect(agentRead).toHaveBeenCalledTimes(2));
    await act(async () => {
      regroupedAgent.resolve(nextAgent);
      regroupedSources.resolve([...sources, newest]);
      await Promise.all([regroupedAgent.promise, regroupedSources.promise]);
    });

    expect(await screen.findByText('Newest source')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Add to order' })).toBeTruthy();
    expect(agentRead).toHaveBeenCalledTimes(2);
  });

  it('moves a persistent composition hole to the drawer F1 retry state', async () => {
    const inconsistent = {
      ...agent,
      sources: {
        order: ['src_a', 'src_deleted'],
        eligibility: [...(agent.sources?.eligibility ?? []), { source_id: 'src_deleted', eligible: true }],
      },
    };
    const agentRead = vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(inconsistent);
    vi.mocked(modelsApi.listSources).mockResolvedValue([sources[0]]);

    renderDrawer();

    expect(await screen.findByText('The source list could not be read')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(agentRead).toHaveBeenCalledTimes(2);
  });

  it('moves a persistent inventory-newer hole to the drawer F1 retry state', async () => {
    const newest = source('src_c', 'Newest source');
    const agentRead = vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    vi.mocked(modelsApi.listSources).mockResolvedValue([...sources, newest]);

    renderDrawer();

    expect(await screen.findByText('The source list could not be read')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(agentRead).toHaveBeenCalledTimes(2);
  });
});
