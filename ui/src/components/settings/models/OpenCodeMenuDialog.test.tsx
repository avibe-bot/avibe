// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
import { OpenCodeMenuDialog } from './OpenCodeMenuDialog';
import type { AgentSupply, Source } from './types';

const source = (overrides: Partial<Source> & Pick<Source, 'id' | 'vendor' | 'display_name'>): Source => ({
  last_discovered_at: '2026-08-29T00:00:00Z',
  kind: 'api_key',
  protocol: 'openai_responses',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
  ...overrides,
});

const sources: Source[] = [
  source({
    id: 'src_openai',
    vendor: 'openai',
    display_name: 'OpenAI Relay',
    models: [
      { id: 'gpt-5.6-luna', origin: 'discovered', reasoning_efforts: [] },
      { id: 'gpt-5.6-sol', origin: 'discovered', reasoning_efforts: [] },
      { id: 'gpt-retired', origin: 'discovered', reasoning_efforts: [], retired: true },
    ],
  }),
  source({
    id: 'src_blocked',
    vendor: 'anthropic',
    display_name: 'Blocked Source',
    protocol: 'anthropic',
    models: [{ id: 'claude-opus-4-8', origin: 'discovered', reasoning_efforts: [] }],
  }),
];

const agent: AgentSupply = {
  backend: 'opencode',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'open',
  sources: {
    order: ['src_openai'],
    eligibility: [
      { source_id: 'src_openai', eligible: true },
      { source_id: 'src_blocked', eligible: false, reason_key: 'models.eligibility.subscription_wrong_client' },
    ],
  },
  routes: {},
  model_supply: [],
  named_agents: [],
  menu: { view: 'featured', checked: [] },
  standard_vendors: ['openai', 'anthropic'],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('OpenCodeMenuDialog', () => {
  it('lists server-eligible inventory and saves the explicit OpenCode selection', async () => {
    const user = userEvent.setup();
    const echoed = {
      ...agent,
      menu: { view: 'featured' as const, checked: ['openai/gpt-5.6-luna'] },
      model_supply: [{ model_id: 'openai/gpt-5.6-luna', chain_length: 0, has_runnable_hop: false }],
    };
    const putMenu = vi.spyOn(modelsApi, 'putMenu').mockResolvedValue(echoed);
    const onSaved = vi.fn();
    const onClose = vi.fn();

    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          agent={agent}
          sources={sources}
          onClose={onClose}
          onSaved={onSaved}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    expect(screen.getByText('0 of 2 selected')).toBeTruthy();
    expect(screen.queryByText('Blocked Source')).toBeNull();
    expect(screen.queryByText(/gpt-retired/)).toBeNull();

    await user.click(screen.getByRole('checkbox', { name: /openai\/gpt-5\.6-luna/i }));
    expect(screen.getByText('1 of 2 selected')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Save selection' }));

    await waitFor(() => expect(putMenu).toHaveBeenCalledWith({
      view: 'featured',
      checked: ['openai/gpt-5.6-luna'],
    }));
    expect(onSaved).toHaveBeenCalledWith(echoed);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('filters by source name without changing the selected menu', async () => {
    const user = userEvent.setup();
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          agent={{ ...agent, menu: { view: 'full', checked: ['openai/gpt-5.6-sol'] } }}
          sources={sources}
          onClose={vi.fn()}
          onSaved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    await user.type(screen.getByRole('textbox', { name: 'Search models or sources' }), 'luna');
    expect(screen.getByRole('checkbox', { name: /openai\/gpt-5\.6-luna/i })).toBeTruthy();
    expect(screen.queryByRole('checkbox', { name: /openai\/gpt-5\.6-sol/i })).toBeNull();
    expect(screen.getByText('1 of 2 selected')).toBeTruthy();
  });

  it('persists removal of a checked identifier whose last eligible supplier disappeared', async () => {
    const user = userEvent.setup();
    const putMenu = vi.spyOn(modelsApi, 'putMenu').mockResolvedValue({ ...agent, menu: { view: 'featured', checked: [] } });
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          agent={{ ...agent, menu: { view: 'featured', checked: ['openai/missing-model'] } }}
          sources={sources}
          onClose={vi.fn()}
          onSaved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    expect(screen.getByText('0 of 2 selected')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Save selection' }));
    await waitFor(() => expect(putMenu).toHaveBeenCalledWith({ view: 'featured', checked: [] }));
  });
});
