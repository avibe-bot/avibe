/* @vitest-environment jsdom */
//
// A Direct backend's card in Settings · Models lists the menu the Session model
// picker shows (the same read), and only whoever may add models (Owner and
// Member, `can_manage_instance`) removes one there or brings it back. Before, the card listed nothing and
// counted the gateway's saved list instead.
import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { ToastProvider } from '@/context/ToastProvider';
import { capabilitiesFor } from '@/lib/testing/instanceRoleCapabilities';
import type { InstanceRole } from '@/lib/sessionInfo';
import { DirectModelList, useDirectModelMenu } from './DirectModelMenu';

// The server's Direct menu and its hidden rows, as the routes answer after each write.
let hidden = new Set<string>();
const offered = ['gpt-6.2-sol', 'gpt-5.5'];
const API = {
  readModelHubAgentCatalogForModelPicker: vi.fn(async () => ({ backend: 'codex', mode: 'direct' })),
  codexModels: vi.fn(async () => ({ ok: true, models: offered.filter((id) => !hidden.has(id)), model_labels: { 'gpt-6.2-sol': 'GPT-6.2-Sol' } })),
  readDirectModelRemovals: vi.fn(async () => ({ ok: true, models: offered.filter((id) => hidden.has(id)).map((id) => ({ id, label: id })) })),
  setDirectModelRemoved: vi.fn(async (_backend: string, id: string, removed: boolean) => {
    if (removed) hidden.add(id); else hidden.delete(id);
    return { ok: true };
  }),
};
vi.mock('@/context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/context/ApiContext')>()),
  useApi: () => API,
}));

const Card = () => <DirectModelList menu={useDirectModelMenu('codex', true)} />;

const renderAs = async (role: InstanceRole) => {
  render(
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: role, capabilities: capabilitiesFor(role) }}>
          <Card />
        </InstanceAuthorizationContext.Provider>
      </ToastProvider>
    </I18nextProvider>,
  );
  await act(async () => {});
};

const listed = () => Array.from(document.querySelectorAll('[data-direct-model]')).map((row) => row.getAttribute('data-direct-model'));

afterEach(() => {
  cleanup();
  hidden = new Set();
  vi.clearAllMocks();
});

describe('Direct model menu in Settings · Models', () => {
  it('lists the picker menu; the Owner removes a model and restores it', async () => {
    await i18n.changeLanguage('en');
    await renderAs('owner');
    expect(listed()).toEqual(['gpt-6.2-sol', 'gpt-5.5']);

    await userEvent.click(screen.getByRole('button', { name: 'Remove GPT-6.2-Sol' }));
    await act(async () => {});
    expect(API.setDirectModelRemoved).toHaveBeenCalledWith('codex', 'gpt-6.2-sol', true);
    expect(listed()).toEqual(['gpt-5.5']);

    await userEvent.click(screen.getByRole('button', { name: '1 removed model' }));
    const restoreRow = document.querySelector('[data-direct-removed-model="gpt-6.2-sol"]') as HTMLElement;
    await userEvent.click(within(restoreRow).getByRole('button', { name: 'Restore gpt-6.2-sol' }));
    await act(async () => {});
    expect(API.setDirectModelRemoved).toHaveBeenLastCalledWith('codex', 'gpt-6.2-sol', false);
    expect(listed()).toEqual(['gpt-6.2-sol', 'gpt-5.5']);
    expect(document.querySelector('[data-direct-removed-toggle]')).toBeNull();
  });

  it('shows a role that cannot add models the same menu with nothing to remove or restore', async () => {
    hidden = new Set(['gpt-5.5']);
    await renderAs('editor');
    expect(listed()).toEqual(['gpt-6.2-sol']);
    expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull();
    expect(document.querySelector('[data-direct-removed-toggle]')).toBeNull();
    expect(API.readDirectModelRemovals).not.toHaveBeenCalled();
  });
});
