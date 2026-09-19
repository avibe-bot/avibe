// @vitest-environment jsdom
//
// The settings migration, which is deliberately broader than setup's key-import
// entry. `keep_native` is a real applied action here — it files the native CLI
// login as a native_cli-channel source — so narrowing the dialog for one caller
// must not quietly narrow it for this one. That, and the row's own presentation
// rules: a provider name only when the server sent the metadata for it, never
// one inferred from whichever backend happened to hold the key.
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { MigrationItem } from './types';

const showToast = vi.hoisted(() => vi.fn());
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));

import { MigrationDialog } from './MigrationDialog';
import { isImportableKey } from './migrationScan';
import { modelsApi } from './modelsApi';

const SUBSCRIPTION: MigrationItem = {
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

const CODEX_KEY: MigrationItem = {
  id: 'mig_codex_key',
  backend: 'codex',
  kind: 'api_key',
  masked_detail: 'sk-…9f21',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'openai',
  display_name: 'OpenAI',
  masked_credential: 'sk-…9f21',
};

// Pre-selected by the scan, and still never batchable: it needs the browser flow.
const REAUTH: MigrationItem = {
  id: 'mig_claude_reauth',
  backend: 'claude',
  kind: 'api_key',
  masked_detail: 'sk-ant-…4b7e',
  proposed_action: 'reauth',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
  masked_credential: 'sk-ant-…4b7e',
};

// An older server that sends none of the presentation metadata.
const LEGACY: MigrationItem = {
  id: 'mig_opencode_legacy',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: '自建中转 · sk-…abcd',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
};

const SCAN = [SUBSCRIPTION, REAUTH, CODEX_KEY, LEGACY];

const applied: string[][] = [];

const serve = (items: MigrationItem[] = SCAN) => {
  vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: items.map((i) => ({ ...i })) });
  vi.spyOn(modelsApi, 'applyMigration').mockImplementation(async (ids) => {
    applied.push([...ids]);
    return { applied: ids.length, sources: [] };
  });
};

const renderDialog = (props: Partial<React.ComponentProps<typeof MigrationDialog>> = {}) =>
  render(
    <I18nextProvider i18n={i18n}>
      <MigrationDialog open onClose={vi.fn()} {...props} />
    </I18nextProvider>,
  );

beforeEach(async () => {
  applied.length = 0;
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  showToast.mockReset();
});

describe('MigrationDialog — the settings default', () => {
  it('shows every scanned row, subscriptions included', async () => {
    serve();
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect(within(dialog).getByText(/Claude 账号登录/)).toBeTruthy();
    expect(within(dialog).getByText('Keep native')).toBeTruthy();
    expect(within(dialog).getByText(/Re-authorize/)).toBeTruthy();
  });

  it('applies a keep_native row — it is a real action here, not a leftover', async () => {
    serve();
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    // Three of the four: everything except the reauth row.
    await user.click(within(dialog).getByRole('button', { name: /Import 3 items/ }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toContain('mig_claude_oauth');
    expect(applied[0]).toEqual(['mig_claude_oauth', 'mig_codex_key', 'mig_opencode_legacy']);
  });

  it('never submits a reauth row, even pre-selected', async () => {
    serve();
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect(within(dialog).getByRole('checkbox', { name: /sk-ant-…4b7e/ })).toHaveProperty('disabled', true);
    await user.click(within(dialog).getByRole('button', { name: /Import 3 items/ }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).not.toContain('mig_claude_reauth');
  });

  it('narrows to a caller-supplied predicate without changing its own default', async () => {
    serve();
    renderDialog({ eligible: isImportableKey });

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect(within(dialog).queryByText(/Claude 账号登录/)).toBeNull();
    expect(within(dialog).queryByText(/Re-authorize/)).toBeNull();
    expect(within(dialog).getByRole('button', { name: /Import 2 items/ })).toBeTruthy();
  });
});

describe('MigrationDialog — how a row names itself', () => {
  it('uses the provider metadata when the server sent it', async () => {
    serve([CODEX_KEY]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    // The masked key and the source sit under the provider, so two keys of the
    // same provider are still tellable apart.
    expect(within(dialog).getByText('sk-…9f21 · Codex configuration')).toBeTruthy();
  });

  it('keeps the composed detail when the server sent none', async () => {
    serve([LEGACY]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    // The composed detail stays the title, with no provider name above it: the
    // assistant the key came from is known, the provider it belongs to is not.
    await waitFor(() => expect(within(dialog).getByText('自建中转 · sk-…abcd')).toBeTruthy());
    expect(
      within(dialog).getByRole('checkbox', { name: '自建中转 · sk-…abcd · OpenCode configuration' }),
    ).toBeTruthy();
  });

  it('names an OpenCode source generically, never a specific store', async () => {
    // `backend` + `kind` cannot tell an opencode.json key from an auth.json one,
    // so the row must not claim either.
    const fromAuthStore: MigrationItem = { ...LEGACY, id: 'mig_zhipu', vendor: 'zhipu', display_name: 'zhipu', masked_credential: 'sk-…3456' };
    serve([fromAuthStore]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('Zhipu AI')).toBeTruthy());
    expect(within(dialog).getByText('sk-…3456 · OpenCode configuration')).toBeTruthy();
    expect(within(dialog).queryByText(/auth\.json|opencode\.json/)).toBeNull();
  });
});
