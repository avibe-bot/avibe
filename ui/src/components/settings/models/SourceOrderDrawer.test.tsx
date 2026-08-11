// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
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
const agent: AgentSupply = {
  backend: 'claude',
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

describe('SourceOrderDrawer keyboard ordering', () => {
  it('grabs, moves, announces, and restores the pre-grab order on Escape', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    renderDrawer();
    const handles = await screen.findAllByRole('button', { name: 'Reorder source' });

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
    await user.click(screen.getByRole('button', { name: 'Save order' }));

    await screen.findByText('The order was not saved');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.getByText('This order is empty. Add a source from below.')).toBeTruthy();
    expect(getAgentSources).toHaveBeenCalledWith('claude');
    expect(putAgentSources).toHaveBeenCalledWith('claude', { order: [] });
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
    expect(screen.getByRole('button', { name: 'Save order' }).hasAttribute('disabled')).toBe(true);
  });
});
