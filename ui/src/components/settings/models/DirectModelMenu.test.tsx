/* @vitest-environment jsdom */
//
// A Direct backend's card in Settings · Models lists the menu its Session
// pickers offer and counts it (before, it listed nothing and counted the
// gateway's saved list). Whoever may add models (`can_manage_instance`) removes
// one there or brings it back; a failed read never takes a row away.
import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { capabilitiesFor } from '@/lib/testing/instanceRoleCapabilities';
import type { InstanceRole } from '@/lib/sessionInfo';
import { CATALOG_REFRESH_RETRY_DELAY_MS } from '@/lib/backendModels';
import { AgentCard } from './AgentCard';
import { modelsApi, type DirectMenu } from './modelsApi';
import type { AgentSupply } from './types';

// The server's Direct menu and the rows it hides, as the route answers after each write.
let hidden = new Set<string>();
const offered = [{ id: 'gpt-6.2-sol', label: 'GPT-6.2-Sol' }, { id: 'gpt-5.5', label: 'gpt-5.5' }];
const answer = (): DirectMenu => ({
  models: offered.filter((row) => !hidden.has(row.id)),
  removed: offered.filter((row) => hidden.has(row.id)),
});

const codex: AgentSupply = {
  backend: 'codex', cli_present: true, mode: 'direct', menu_kind: 'fixed', selected_model_id: null, selected_model_explicit: false,
  sources: null, routes: null, supply_status: null, model_supply: null, named_agents: [], builtin_models: null, menu: null,
  // The gateway's saved list, which a Direct card must not count or show.
  catalog_models: [],
};

const renderAs = async (role: InstanceRole) => {
  render(
    <I18nextProvider i18n={i18n}>
      <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: role, capabilities: capabilitiesFor(role) }}>
        <AgentCard
          agents={[codex]}
          runtime={null}
          sources={[]}
          chains={{}}
          pendingBackends={new Set()}
          switchFailures={new Set()}
          connectingBackend={null}
          onConnectHub={vi.fn()}
          onSwitchDirect={vi.fn()}
          onOpenModels={vi.fn()}
          onAddModels={vi.fn()}
          onOpenOrder={vi.fn()}
          onOpenRoute={vi.fn()}
          onProbeSettled={vi.fn()}
        />
      </InstanceAuthorizationContext.Provider>
    </I18nextProvider>,
  );
  await act(async () => {});
};

const listed = () => Array.from(document.querySelectorAll('[data-direct-model]')).map((row) => row.getAttribute('data-direct-model'));

beforeEach(async () => {
  await i18n.changeLanguage('en');
  vi.spyOn(modelsApi, 'getDirectMenu').mockImplementation(async () => answer());
  vi.spyOn(modelsApi, 'setDirectModelRemoved').mockImplementation(async (_backend, id, removed) => {
    if (removed) hidden.add(id); else hidden.delete(id);
  });
});

afterEach(() => {
  cleanup();
  hidden = new Set();
  vi.restoreAllMocks();
});

describe('Direct model menu in Settings · Models', () => {
  it('lists and counts the picker menu; removing and restoring round-trip', async () => {
    await renderAs('owner');
    expect(listed()).toEqual(['gpt-6.2-sol', 'gpt-5.5']);
    expect(screen.getByText('2 models')).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: 'Remove GPT-6.2-Sol' }));
    await act(async () => {});
    expect(modelsApi.setDirectModelRemoved).toHaveBeenCalledWith('codex', 'gpt-6.2-sol', true);
    expect(listed()).toEqual(['gpt-5.5']);

    await userEvent.click(screen.getByRole('button', { name: '1 removed model' }));
    const restoreRow = document.querySelector('[data-direct-removed-model="gpt-6.2-sol"]') as HTMLElement;
    await userEvent.click(within(restoreRow).getByRole('button', { name: 'Restore GPT-6.2-Sol' }));
    await act(async () => {});
    expect(modelsApi.setDirectModelRemoved).toHaveBeenLastCalledWith('codex', 'gpt-6.2-sol', false);
    expect(listed()).toEqual(['gpt-6.2-sol', 'gpt-5.5']);
    expect(document.querySelector('[data-direct-removed-toggle]')).toBeNull();
  });

  it('keeps the menu and the restore list it has when a later read fails', async () => {
    hidden = new Set(['gpt-5.5']);
    await renderAs('owner');
    vi.mocked(modelsApi.getDirectMenu).mockRejectedValueOnce(new Error('offline'));
    await userEvent.click(screen.getByRole('button', { name: 'Remove GPT-6.2-Sol' }));
    await act(async () => {});
    expect(listed()).toEqual(['gpt-6.2-sol']);
    expect(screen.getByRole('button', { name: '1 removed model' })).toBeTruthy();
  });

  it('reads again while a remote catalog refresh is pending, as the Session pickers do', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(modelsApi.getDirectMenu).mockImplementationOnce(async () => ({ ...answer(), models: [offered[1]], catalogRefreshPending: true }));
      await renderAs('owner');
      expect(listed()).toEqual(['gpt-5.5']);
      await act(async () => { await vi.advanceTimersByTimeAsync(CATALOG_REFRESH_RETRY_DELAY_MS); });
      expect(listed()).toEqual(['gpt-6.2-sol', 'gpt-5.5']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows a role that cannot add models the same menu with nothing to remove or restore', async () => {
    hidden = new Set(['gpt-5.5']);
    await renderAs('editor');
    expect(listed()).toEqual(['gpt-6.2-sol']);
    expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull();
    expect(document.querySelector('[data-direct-removed-toggle]')).toBeNull();
  });
});
