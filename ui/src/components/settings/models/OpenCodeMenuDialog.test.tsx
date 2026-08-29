// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { ApiCallError, modelsApi } from './modelsApi';
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
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(agent);
    const putMenu = vi.spyOn(modelsApi, 'putMenu').mockResolvedValue(echoed);
    const onSaved = vi.fn();
    const onClose = vi.fn();

    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          sourceReads={{ readValue: vi.fn().mockResolvedValue(sources) }}
          onClose={onClose}
          onSaved={onSaved}
          onObserved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    expect(await screen.findByText('0 of 2 selected')).toBeTruthy();
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
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue({
      ...agent,
      menu: { view: 'full', checked: ['openai/gpt-5.6-sol'] },
    });
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          sourceReads={{ readValue: vi.fn().mockResolvedValue(sources) }}
          onClose={vi.fn()}
          onSaved={vi.fn()}
          onObserved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    await user.type(await screen.findByRole('textbox', { name: 'Search models or sources' }), 'luna');
    expect(screen.getByRole('checkbox', { name: /openai\/gpt-5\.6-luna/i })).toBeTruthy();
    expect(screen.queryByRole('checkbox', { name: /openai\/gpt-5\.6-sol/i })).toBeNull();
    expect(screen.getByText('1 of 2 selected')).toBeTruthy();
  });

  it('renders a checked route alias and removes it only after an explicit uncheck', async () => {
    const user = userEvent.setup();
    const configured = {
      ...agent,
      menu: { view: 'featured' as const, checked: ['openai/menu-model'] },
      routes: { 'openai/menu-model': { hops: [{ source_id: 'src_openai', model_id: 'gpt-5.6-luna' }] } },
    };
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(configured);
    const putMenu = vi.spyOn(modelsApi, 'putMenu').mockResolvedValue({ ...agent, menu: { view: 'featured', checked: [] } });
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          sourceReads={{ readValue: vi.fn().mockResolvedValue(sources) }}
          onClose={vi.fn()}
          onSaved={vi.fn()}
          onObserved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    const alias = await screen.findByRole('checkbox', { name: /openai\/menu-model/i });
    expect(alias.getAttribute('aria-checked')).toBe('true');
    expect(screen.getByText('1 of 3 selected')).toBeTruthy();
    await user.click(alias);
    await user.click(screen.getByRole('button', { name: 'Save selection' }));
    await waitFor(() => expect(putMenu).toHaveBeenCalledWith({ view: 'featured', checked: [] }));
  });

  it('preserves a checked cross-model route while saving an unrelated addition', async () => {
    const user = userEvent.setup();
    const configured = {
      ...agent,
      menu: { view: 'featured' as const, checked: ['openai/menu-model'] },
      routes: { 'openai/menu-model': { hops: [{ source_id: 'src_openai', model_id: 'gpt-5.6-sol' }] } },
    };
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(configured);
    const putMenu = vi.spyOn(modelsApi, 'putMenu').mockResolvedValue({
      ...configured,
      menu: { view: 'featured', checked: ['openai/menu-model', 'openai/gpt-5.6-luna'] },
    });
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          sourceReads={{ readValue: vi.fn().mockResolvedValue(sources) }}
          onClose={vi.fn()}
          onSaved={vi.fn()}
          onObserved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    await user.click(await screen.findByRole('checkbox', { name: /openai\/gpt-5\.6-luna/i }));
    await user.click(screen.getByRole('button', { name: 'Save selection' }));
    await waitFor(() => expect(putMenu).toHaveBeenCalledWith({
      view: 'featured',
      checked: ['openai/menu-model', 'openai/gpt-5.6-luna'],
    }));
  });

  it('blocks editing when its dedicated baseline read fails', async () => {
    vi.spyOn(modelsApi, 'getAgentSources').mockRejectedValue(new Error('unread'));
    const putMenu = vi.spyOn(modelsApi, 'putMenu');
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          sourceReads={{ readValue: vi.fn().mockResolvedValue(sources) }}
          onClose={vi.fn()}
          onSaved={vi.fn()}
          onObserved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    expect(await screen.findByText('Model menu data is not current. Wait for refresh, or retry the failed section before editing.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Save selection' }).hasAttribute('disabled')).toBe(true);
    expect(putMenu).not.toHaveBeenCalled();
  });

  it('accepts a committed menu after an inconclusive PUT by re-reading it', async () => {
    const user = userEvent.setup();
    const committed = {
      ...agent,
      menu: { view: 'featured' as const, checked: ['openai/gpt-5.6-luna'] },
    };
    vi.spyOn(modelsApi, 'getAgentSources')
      .mockResolvedValueOnce(agent)
      .mockResolvedValueOnce(agent)
      .mockResolvedValueOnce(committed);
    vi.spyOn(modelsApi, 'putMenu').mockRejectedValue(new ApiCallError('bad_response', undefined, false));
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          sourceReads={{ readValue: vi.fn().mockResolvedValue(sources) }}
          onClose={onClose}
          onSaved={onSaved}
          onObserved={vi.fn()}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    await user.click(await screen.findByRole('checkbox', { name: /openai\/gpt-5\.6-luna/i }));
    await user.click(screen.getByRole('button', { name: 'Save selection' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(committed));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('rebases a failed draft on the observed menu before enabling retry', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources')
      .mockResolvedValueOnce(agent)
      .mockResolvedValueOnce(agent)
      .mockResolvedValueOnce(agent);
    vi.spyOn(modelsApi, 'putMenu').mockRejectedValue(new ApiCallError('bad_response', undefined, false));
    const onObserved = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <OpenCodeMenuDialog
          open
          sourceReads={{ readValue: vi.fn().mockResolvedValue(sources) }}
          onClose={vi.fn()}
          onSaved={vi.fn()}
          onObserved={onObserved}
          menuWrite={{ pending: false, track: async (work) => work() }}
        />
      </I18nextProvider>,
    );

    const luna = await screen.findByRole('checkbox', { name: /openai\/gpt-5\.6-luna/i });
    await user.click(luna);
    await user.click(screen.getByRole('button', { name: 'Save selection' }));

    expect(await screen.findByText('The model selection was not saved')).toBeTruthy();
    expect(onObserved).toHaveBeenCalledWith(agent);
    expect(luna.getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('button', { name: 'Save selection' }).hasAttribute('disabled')).toBe(false);
  });
});
