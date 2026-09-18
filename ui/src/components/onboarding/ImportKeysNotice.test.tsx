// @vitest-environment jsdom
//
// The setup import entry, driven through the real dialog it opens. Only the two
// boundaries are stubbed — the HTTP calls and the capability probe — so what is
// under test is the thing that actually matters here: that the number in the
// sentence, the rows a person is offered, and the ids finally submitted are all
// the same set. A count computed from one rule and rows rendered from another is
// the failure this fixture exists to catch.
//
// The scan fixture carries the shapes `migration.py` really produces, including
// a row with no presentation metadata at all (an older server) and non-ASCII
// detail text.
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { MigrationItem } from '@/components/settings/models/types';

const showToast = vi.hoisted(() => vi.fn());
const capability = vi.hoisted(() => ({ value: true as boolean | null }));

vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('@/components/settings/models/useModelHubCapability', () => ({
  useModelHubCapability: () => capability.value,
}));

import { modelsApi } from '@/components/settings/models/modelsApi';
import { ImportKeysNotice } from './ImportKeysNotice';

const CLAUDE_KEY: MigrationItem = {
  id: 'mig_claude_key',
  backend: 'claude',
  kind: 'api_key',
  masked_detail: 'sk-…dd3c',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
  masked_credential: 'sk-…dd3c',
};

// A native subscription. Legitimately applied by the settings migration; never
// part of this entry.
const CLAUDE_SUBSCRIPTION: MigrationItem = {
  id: 'mig_claude_oauth',
  backend: 'claude',
  kind: 'oauth_native',
  masked_detail: 'Claude 账号登录（OAuth）',
  proposed_action: 'keep_native',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
  masked_credential: null,
};

// Key-shaped, but needs the interactive browser flow — not batchable.
const CODEX_REAUTH: MigrationItem = {
  id: 'mig_codex_reauth',
  backend: 'codex',
  kind: 'api_key',
  masked_detail: 'sk-…9f21',
  proposed_action: 'reauth',
  selected: true,
  notes_key: null,
  vendor: 'openai',
  display_name: 'OpenAI',
  masked_credential: 'sk-…9f21',
};

// An OpenCode key: `opencode_provider`, never `api_key`, and its display_name is
// only the provider id echoed back.
const OPENCODE_ZHIPU: MigrationItem = {
  id: 'mig_opencode_zhipu',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: 'zhipu · sk-…3456',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'zhipu',
  display_name: 'zhipu',
  masked_credential: 'sk-…3456',
};

// An older server, or a provider nothing recognises: no presentation metadata at
// all, so the row has only its composed detail to show.
const OPENCODE_LEGACY: MigrationItem = {
  id: 'mig_opencode_legacy',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: '自建中转 · sk-…abcd',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
};

const FULL_SCAN = [CLAUDE_KEY, CLAUDE_SUBSCRIPTION, CODEX_REAUTH, OPENCODE_ZHIPU, OPENCODE_LEGACY];

// A fake server rather than a queue of canned replies: the notice and the dialog
// it opens each scan on their own schedule, so a fixed reply order would decide
// which surface saw what. Applying removes the imported rows, which is what makes
// the notice's post-import remainder a real answer instead of arithmetic.
let stored: MigrationItem[] = [];
const applied: string[][] = [];

const serve = (items: MigrationItem[]) => {
  stored = items.map((i) => ({ ...i }));
  vi.spyOn(modelsApi, 'scanMigration').mockImplementation(async () => ({
    items: stored.map((i) => ({ ...i })),
  }));
  vi.spyOn(modelsApi, 'applyMigration').mockImplementation(async (ids) => {
    applied.push([...ids]);
    stored = stored.filter((i) => !ids.includes(i.id));
    return { applied: ids.length, sources: [] };
  });
};

const renderNotice = (onApplied?: (applied: number) => void) =>
  render(
    <I18nextProvider i18n={i18n}>
      <ImportKeysNotice onApplied={onApplied} />
    </I18nextProvider>,
  );

const openDialog = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole('button', { name: 'Review and import' }));
  return screen.findByRole('dialog');
};

beforeEach(async () => {
  capability.value = true;
  applied.length = 0;
  window.localStorage.clear();
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  showToast.mockReset();
});

describe('ImportKeysNotice', () => {
  it('counts the importable keys — not the rows the scan returned', async () => {
    serve(FULL_SCAN);
    renderNotice();

    // Three of five: the subscription and the reauth row are not offers.
    expect(await screen.findByText('Found 3 API keys to import into Model Gateway')).toBeTruthy();
  });

  it('offers exactly the rows it counted, and no subscription or reauth row', async () => {
    serve(FULL_SCAN);
    renderNotice();
    const user = userEvent.setup();

    const dialog = await openDialog(user);

    expect(within(dialog).getByText('Anthropic')).toBeTruthy();
    // `display_name` was only the provider id; the shared brand mapping names it.
    expect(within(dialog).getByText('Zhipu AI')).toBeTruthy();
    // No metadata at all: the composed detail stays the title rather than a
    // provider guessed from the backend that held the key.
    expect(within(dialog).getByText('自建中转 · sk-…abcd')).toBeTruthy();

    expect(within(dialog).queryByText('Claude 账号登录（OAuth）')).toBeNull();
    expect(within(dialog).queryByText(/Keep native/)).toBeNull();
    expect(within(dialog).queryByText(/Re-authorize/)).toBeNull();
    expect(within(dialog).getByRole('button', { name: /Import 3 items/ })).toBeTruthy();
  });

  it('submits only the rows still ticked', async () => {
    serve(FULL_SCAN);
    const onApplied = vi.fn();
    renderNotice(onApplied);
    const user = userEvent.setup();

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole('checkbox', { name: /Anthropic/ }));
    await user.click(within(dialog).getByRole('button', { name: /Import 2 items/ }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual(['mig_opencode_zhipu', 'mig_opencode_legacy']);
    expect(onApplied).toHaveBeenCalledWith(2);
  });

  it('stays to report what was imported and what is still available', async () => {
    serve(FULL_SCAN);
    renderNotice();
    const user = userEvent.setup();

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole('checkbox', { name: /Anthropic/ }));
    await user.click(within(dialog).getByRole('button', { name: /Import 2 items/ }));

    // The remainder is the server's answer after the rescan, not a subtraction.
    expect(await screen.findByText('Imported 2 · 1 API key still available')).toBeTruthy();
    // A partial selection can still be finished from here.
    expect(screen.getByRole('button', { name: 'Review and import' })).toBeTruthy();
  });

  it('reports a finished import with no review action left', async () => {
    serve(FULL_SCAN);
    renderNotice();
    const user = userEvent.setup();

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole('button', { name: /Import 3 items/ }));

    expect(await screen.findByText('Imported 3 API keys into Model Gateway')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Review and import' })).toBeNull();
    // The subscription and the reauth row are still on this machine, untouched.
    expect(stored.map((i) => i.id)).toEqual(['mig_claude_oauth', 'mig_codex_reauth']);
  });

  it('remembers a dismissal so the same keys do not ask again', async () => {
    serve(FULL_SCAN);
    const { unmount } = renderNotice();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Dismiss import notice' }));
    expect(screen.queryByText(/API keys to import/)).toBeNull();

    unmount();
    cleanup();
    renderNotice();
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/API keys to import/)).toBeNull();
  });

  it('asks again when a genuinely new key appears', async () => {
    serve(FULL_SCAN);
    const { unmount } = renderNotice();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Dismiss import notice' }));
    unmount();
    cleanup();
    stored = [...stored, { ...CLAUDE_KEY, id: 'mig_claude_key_2', masked_detail: 'sk-…7a10' }];
    renderNotice();

    expect(await screen.findByText('Found 4 API keys to import into Model Gateway')).toBeTruthy();
  });

  it('says nothing when there is nothing to import', async () => {
    serve([CLAUDE_SUBSCRIPTION, CODEX_REAUTH]);
    renderNotice();

    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalled());
    expect(screen.queryByText(/Model Gateway/)).toBeNull();
  });

  it('says nothing when the scan fails', async () => {
    // Advertising rows after a failed scan would open a dialog that rescans,
    // fails again and shows nothing.
    vi.spyOn(modelsApi, 'scanMigration').mockRejectedValue(new Error('offline'));
    renderNotice();

    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalled());
    expect(screen.queryByText(/Model Gateway/)).toBeNull();
  });

  it('does not scan at all when the gateway capability is off', async () => {
    capability.value = false;
    serve(FULL_SCAN);
    renderNotice();

    await Promise.resolve();
    expect(modelsApi.scanMigration).not.toHaveBeenCalled();
    expect(screen.queryByText(/Model Gateway/)).toBeNull();
  });
});
