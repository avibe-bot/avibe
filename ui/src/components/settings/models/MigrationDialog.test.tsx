// @vitest-environment jsdom
//
// The settings migration and setup's key-import entry share the same takeover
// dialog. A backend is one custody boundary: unsupported rows stay visible and
// block that CLI, while a complete backend can still migrate independently.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { MigrationItem } from './types';

const showToast = vi.hoisted(() => vi.fn());
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));

import { MigrationDialog } from './MigrationDialog';
import { isImportableKey } from './migrationScan';
import { ApiCallError, modelsApi } from './modelsApi';

const SUBSCRIPTION: MigrationItem = {
  id: 'mig_claude_oauth',
  backend: 'claude',
  kind: 'oauth_native',
  masked_detail: 'Claude 账号登录（OAuth）',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
  masked_credential: null,
};

const SUBSCRIPTION_2: MigrationItem = {
  ...SUBSCRIPTION,
  id: 'mig_claude_oauth_2',
  masked_detail: 'Claude 账号登录（OAuth）· 第二账号',
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

// Pre-selected by the scan, but not batchable: it needs the browser flow.
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
  it('shows blocked native rows and keeps a complete backend actionable', async () => {
    serve();
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect(within(dialog).getByText(/Claude 账号登录/)).toBeTruthy();
    expect((within(dialog).getByRole('checkbox', { name: /sk-ant-…4b7e/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(dialog).getByText('Use the existing Add flow for this CLI, then scan again.')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('applies only complete backend groups with one migration action', async () => {
    serve();
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual(['mig_codex_key', 'mig_opencode_legacy']);
    expect(applied[0]).not.toContain('mig_claude_oauth');
  });

  it('selects and submits every import candidate for a backend as one group', async () => {
    serve([SUBSCRIPTION, SUBSCRIPTION_2, CODEX_KEY, LEGACY]);
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    const claudeRows = within(dialog).getAllByRole('checkbox').slice(0, 2);
    expect(claudeRows).toHaveLength(2);
    await user.click(claudeRows[0]);
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual(['mig_codex_key', 'mig_opencode_legacy']);
    expect(applied[0]).not.toContain('mig_claude_oauth');
    expect(applied[0]).not.toContain('mig_claude_oauth_2');
  });

  it('shows but never submits a reauth row, even when it is pre-selected', async () => {
    serve();
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect((within(dialog).getByRole('checkbox', { name: /sk-ant-…4b7e/ }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).not.toContain('mig_claude_reauth');
  });

  it('uses eligible as a backend scope without hiding blocked rows in that backend', async () => {
    const claudeKey: MigrationItem = {
      ...CODEX_KEY,
      id: 'mig_claude_key',
      backend: 'claude',
      masked_detail: 'sk-ant-…1234',
      vendor: 'anthropic',
      display_name: 'Anthropic',
      masked_credential: 'sk-ant-…1234',
    };
    serve([claudeKey, SUBSCRIPTION, REAUTH]);
    renderDialog({ eligible: (item) => item.backend === 'claude' && isImportableKey(item) });

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getAllByText('Anthropic').length).toBeGreaterThan(0));
    expect(within(dialog).getByText(/Claude 账号登录/)).toBeTruthy();
    expect((within(dialog).getByRole('checkbox', { name: /sk-ant-…4b7e/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(dialog).getByRole('button', { name: 'Start migration' })).toBeTruthy();
  });

  it('closes without applying when the user chooses Not now', async () => {
    serve();
    const onClose = vi.fn();
    renderDialog({ onClose });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    await user.click(within(dialog).getByRole('button', { name: 'Later' }));

    expect(onClose).toHaveBeenCalledOnce();
    expect(applied).toEqual([]);
  });

  it('keeps the selection after an apply failure and allows retry', async () => {
    vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [{ ...CODEX_KEY }] });
    vi.spyOn(modelsApi, 'applyMigration')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ applied: 1, sources: [] });
    const onClose = vi.fn();
    renderDialog({ onClose });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    const start = within(dialog).getByRole('button', { name: 'Start migration' });
    await user.click(start);

    await waitFor(() => expect(showToast).toHaveBeenCalledWith('Migration failed, please retry', 'error'));
    expect(onClose).not.toHaveBeenCalled();
    expect((start as HTMLButtonElement).disabled).toBe(false);

    await user.click(start);
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(modelsApi.applyMigration).toHaveBeenCalledTimes(2);
  });

  it('shows the migrating state while apply is waiting', async () => {
    const pending = new Promise<{ applied: number; sources: never[] }>(() => {});
    vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [{ ...CODEX_KEY }] });
    vi.spyOn(modelsApi, 'applyMigration').mockReturnValue(pending);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect((within(dialog).getByRole('button', { name: 'Migrating…' }) as HTMLButtonElement).disabled).toBe(true));
  });

  it.each([
    ['migration_native_busy', 'Close the CLI or finish its tasks, then retry.'],
    ['migration_permission_needed', 'Allow credential access, then retry.'],
    ['migration_recovery_pending', 'Migration is unfinished. Retry to continue.'],
    ['migration_item_conflict', 'The native configuration changed. Scan again.'],
    ['migration_configuration_blocked', 'Adjust the native configuration, then scan again.'],
  ] as const)('maps %s to concise localized copy', async (code, message) => {
    vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [{ ...CODEX_KEY }] });
    vi.spyOn(modelsApi, 'applyMigration').mockRejectedValue(new ApiCallError(code));
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(message, 'error'));
  });

  it.each([
    ['en', 'Authentication has expired. Sign in again in Model Hub.'],
    ['zh', '认证已过期，请在模型网关中重新登录。'],
  ] as const)('maps expired migration credentials to the %s Hub reauthentication action', async (language, message) => {
    await i18n.changeLanguage(language);
    vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [{ ...CODEX_KEY }] });
    vi.spyOn(modelsApi, 'applyMigration').mockRejectedValue(new ApiCallError('migration_credentials_invalid'));
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    await user.click(within(dialog).getByRole('button', { name: language === 'en' ? 'Start migration' : '开始迁移' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(message, 'error'));
  });

  it.each([
    ['settings.models.migration.blocked.config', "Adjust this CLI's configuration, then scan again."],
    ['settings.models.migration.blocked.environment', 'Close the CLI or finish its current task, then retry.'],
    ['settings.models.migration.blocked.credential', 'Allow credential access, then retry.'],
    ['settings.models.migration.blocked.unknown', 'Use the existing Add flow for this CLI, then scan again.'],
  ] as const)('maps blocked note %s to actionable copy', async (notes_key, message) => {
    const blocked = { ...REAUTH, notes_key };
    serve([CODEX_KEY, blocked]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect((within(dialog).getByRole('status') as HTMLElement).textContent).toContain(message);
  });
});

describe('MigrationDialog — approved copy', () => {
  it.each([
    ['en', 'After migration, CLI authentication will be fully managed by Model Hub.'],
    ['zh', '迁移后，CLI的认证信息将完全交由模型网关管理'],
  ] as const)('renders the %s consequence sentence exactly', async (language, sentence) => {
    await i18n.changeLanguage(language);
    serve([CODEX_KEY]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(sentence)).toBeTruthy();
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
