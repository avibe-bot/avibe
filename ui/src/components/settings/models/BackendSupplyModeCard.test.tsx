// @vitest-environment jsdom
//
// Migration is optional on the way to the gateway: declining the takeover the
// card offers still makes the switch, while declining an import asked for from
// the Direct strip leaves the backend on Direct.
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AgentSupply, MigrationItem, RuntimeDependency } from './types';

const showToast = vi.hoisted(() => vi.fn());
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));

import { BackendSupplyModeCard } from './BackendSupplyModeCard';
import { modelsApi } from './modelsApi';

const DIRECT: AgentSupply = { backend: 'claude', cli_present: true, mode: 'direct', menu_kind: 'fixed' };

const CLAUDE_KEY: MigrationItem = {
  id: 'mig_claude_key',
  backend: 'claude',
  kind: 'api_key',
  masked_detail: 'sk-ant-…1c05',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
  masked_credential: 'sk-ant-…1c05',
};

const RUNTIME: RuntimeDependency = {
  contract_version: 10,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1', source_sha: 'a'.repeat(40), assets: [] },
  status: { installed_version: '1', verified: true, health: 'ok' },
};

beforeEach(async () => {
  await i18n.changeLanguage('en');
  vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([DIRECT]);
  vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(RUNTIME);
  vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [CLAUDE_KEY] });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  showToast.mockReset();
});

const renderCard = () =>
  render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <BackendSupplyModeCard backend="claude" />
      </I18nextProvider>
    </MemoryRouter>,
  );

describe('BackendSupplyModeCard', () => {
  it.each([
    ['switching to the gateway', /Gateway mode/, true],
    ['importing from the Direct strip', null, false],
  ] as const)('declining the migration offered while %s', async (_, radio, switches) => {
    const apply = vi.spyOn(modelsApi, 'applyMigration');
    const setMode = vi.spyOn(modelsApi, 'setAgentMode').mockResolvedValue({ ...DIRECT, mode: 'hub' });
    renderCard();

    if (radio) await userEvent.click(await screen.findByRole('radio', { name: radio }));
    else await userEvent.click(await screen.findByRole('button', { name: /Import to gateway/ }));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('Anthropic')).toBeTruthy());
    expect(setMode).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Later' }));

    if (switches) {
      await waitFor(() => expect(setMode).toHaveBeenCalledWith('claude', 'hub'));
      await waitFor(() => expect(showToast).toHaveBeenCalledWith('Switched to Gateway', 'success'));
    } else {
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(setMode).not.toHaveBeenCalled();
    }
    expect(apply).not.toHaveBeenCalled();
  });
});
