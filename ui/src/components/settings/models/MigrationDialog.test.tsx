// @vitest-environment jsdom
//
// The settings migration and setup's key-import entry share the same takeover
// dialog. Rows the Hub cannot carry are hidden and stay native; every carried
// row of a backend still migrates together.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { MigrationItem } from './types';

const showToast = vi.hoisted(() => vi.fn());
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));

import { MigrationDialog } from './MigrationDialog';
import type { MigrationSelection } from './migrationGrouping';
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

// Importable, and on the same backend as the subscription above: the pair is what
// setup can see but cannot take, because the server migrates a backend whole.
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
  it('hides rows the Hub cannot carry and keeps every backend actionable', async () => {
    serve();
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect(within(dialog).getByText(/Claude 账号登录/)).toBeTruthy();
    expect(within(dialog).queryByRole('checkbox', { name: /sk-ant-…4b7e/ })).toBeNull();
    expect(within(dialog).queryByRole('status')).toBeNull();
    expect((within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('applies every carried row with one migration action, leaving the rest native', async () => {
    serve();
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual(['mig_claude_oauth', 'mig_codex_key', 'mig_opencode_legacy']);
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

  it('never shows or submits a reauth row, even when it is pre-selected', async () => {
    serve();
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect(within(dialog).queryByText(/sk-ant-…4b7e/)).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).not.toContain('mig_claude_reauth');
  });

  it('uses eligible as a backend scope and still hides rows it cannot carry', async () => {
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
    expect(within(dialog).queryByText(/sk-ant-…4b7e/)).toBeNull();
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
    ['migration_item_conflict', 'Migration could not verify the saved configuration or its credentials. Check the files and authentication before retrying, or add the source manually in Model Hub.'],
    ['migration_configuration_blocked', 'Adjust the native configuration, then scan again.'],
    ['migration_reauthorization_required', 'This backend was already migrated, and the changed native login cannot be verified as a new authorization. The original files are unchanged. Sign in again for the existing source in Model Hub instead of importing this login.'],
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

  it('keeps selection and explains configuration or credential verification failure in Chinese', async () => {
    await i18n.changeLanguage('zh');
    const onClose = vi.fn();
    vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [{ ...CODEX_KEY }] });
    vi.spyOn(modelsApi, 'applyMigration').mockRejectedValue(new ApiCallError('migration_item_conflict'));
    renderDialog({ onClose });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    await user.click(within(dialog).getByRole('button', { name: '开始迁移' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      '无法验证已保存的配置或凭据。请检查配置文件和认证是否有效后重试，或在模型网关中手动添加供应商。',
      'error',
    ));
    expect(onClose).not.toHaveBeenCalled();
    expect(within(dialog).getByRole('checkbox').getAttribute('aria-checked')).toBe('true');
    expect((within(dialog).getByRole('button', { name: '开始迁移' }) as HTMLButtonElement).disabled).toBe(false);
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

  it('refreshes through onApplied and closes after terminal credential settlement', async () => {
    const onApplied = vi.fn();
    const onClose = vi.fn();
    vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [{ ...CODEX_KEY }] });
    vi.spyOn(modelsApi, 'applyMigration').mockRejectedValue(new ApiCallError('migration_credentials_invalid'));
    renderDialog({ onApplied, onClose });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'Authentication has expired. Sign in again in Model Hub.',
      'error',
    ));
    expect(onApplied).toHaveBeenCalledWith(0);
    expect(onClose).toHaveBeenCalledOnce();
    expect(showToast).not.toHaveBeenCalledWith('Migration complete', 'success');
  });

  it('keeps the dialog open while recovery is still pending', async () => {
    const onApplied = vi.fn();
    const onClose = vi.fn();
    vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [{ ...CODEX_KEY }] });
    vi.spyOn(modelsApi, 'applyMigration').mockRejectedValue(new ApiCallError('migration_recovery_pending'));
    renderDialog({ onApplied, onClose });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'Migration is unfinished. Retry to continue.',
      'error',
    ));
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(onApplied).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('says nothing about a row it cannot carry', async () => {
    serve([CODEX_KEY, { ...REAUTH, notes_key: 'settings.models.migration.blocked.config' }]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect(within(dialog).queryByRole('status')).toBeNull();
    expect(within(dialog).getAllByRole('checkbox')).toHaveLength(1);
  });
});

describe('MigrationDialog — persisted authentication', () => {
  it.each([
    ['en', 'Import authentication saved in configuration files, shell startup files, or supported credential stores, without reading runtime environment values. After migration, Model Hub will manage the selected authentication.'],
    ['zh', '导入配置文件、Shell 启动文件或受支持凭据库中已保存的认证，不读取运行中的环境变量。迁移后，所选认证由模型网关管理。'],
  ] as const)('explains the saved source boundary and consequence in %s', async (language, sentence) => {
    await i18n.changeLanguage(language);
    serve([CODEX_KEY]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(sentence)).toBeTruthy();
  });

  it.each(['dynamic_shell', 'ambiguous_shell', 'unreadable', 'reference', 'helper', 'token', 'headers', 'transport'])(
    'hides a %s row and still migrates the backend beside it',
    async (reason) => {
      serve([
        { ...REAUTH, notes_key: `settings.models.migration.blocked.${reason}`, source_paths: ['/home/用户/.zshrc'] },
        { ...CODEX_KEY, source_paths: ['/home/用户/.codex/auth.json'] },
      ]);
      renderDialog();
      const user = userEvent.setup();

      const dialog = await screen.findByRole('dialog');
      await within(dialog).findByText('OpenAI');
      expect(within(dialog).queryByRole('status')).toBeNull();
      expect(within(dialog).queryByText('/home/用户/.zshrc')).toBeNull();
      await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));
      await waitFor(() => expect(applied).toEqual([[CODEX_KEY.id]]));
    },
  );

  it.each([
    ['en', 'No configuration found to migrate.', 'Start migration'],
    ['zh', '没有检测到可迁移的配置。', '开始迁移'],
  ] as const)('renders an empty %s scan without fake credential rows or warnings', async (language, emptyMessage, action) => {
    await i18n.changeLanguage(language);
    serve([]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(emptyMessage)).toBeTruthy();
    expect(within(dialog).queryAllByRole('checkbox')).toHaveLength(0);
    expect(within(dialog).queryAllByRole('status')).toHaveLength(0);
    expect((within(dialog).getByRole('button', { name: action }) as HTMLButtonElement).disabled).toBe(true);
    expect(modelsApi.applyMigration).not.toHaveBeenCalled();
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

  it('shows each explicit file locator for an importable source', async () => {
    const paths = [
      '/home/用户/配置 文件夹/very-long-project-name/.config/opencode/opencode.json',
      '/home/用户/.zshrc',
    ];
    serve([{ ...LEGACY, source_paths: [...paths, paths[0]] }]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    const checkbox = await within(dialog).findByRole('checkbox', {
      name: '自建中转 · sk-…abcd · ' + paths.join(' · '),
    });
    expect((checkbox as HTMLButtonElement).disabled).toBe(false);
    expect(checkbox.getAttribute('aria-checked')).toBe('true');
    for (const path of paths) {
      expect(within(dialog).getAllByText(path)).toHaveLength(1);
    }
    expect(within(dialog).queryByText('OpenCode configuration')).toBeNull();
  });

  it('uses the legacy source description when file metadata is empty', async () => {
    serve([{ ...CODEX_KEY, source_paths: [] }]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('sk-…9f21 · Codex configuration')).toBeTruthy();
    expect(within(dialog).getByRole('checkbox', { name: 'OpenAI · sk-…9f21 · Codex configuration' })).toBeTruthy();
  });
});

describe('MigrationDialog — shared persisted files', () => {
  const linked: MigrationItem[] = [
    { ...CODEX_KEY, source_paths: ['/home/用户/.profile'], required_backends: ['codex', 'opencode'] },
    { ...LEGACY, source_paths: ['/home/用户/.profile'], required_backends: ['codex', 'opencode'] },
    {
      ...LEGACY,
      id: 'mig_opencode_second',
      masked_detail: 'Second source · sk-…5678',
      source_paths: ['/home/用户/.config/opencode/auth.json'],
      required_backends: ['codex', 'opencode'],
    },
  ];

  it.each(['en', 'zh'] as const)('expands entry-point scope and couples every row in the linked group in %s', async (language) => {
    await i18n.changeLanguage(language);
    serve([...linked, SUBSCRIPTION]);
    renderDialog({ eligible: (item) => item.id === CODEX_KEY.id });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    const checkboxes = within(dialog).getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(3);
    expect(within(dialog).queryByText('Anthropic')).toBeNull();
    const sharedMessage = language === 'en'
      ? /share authentication saved in files.*must migrate together: Codex, OpenCode.*Changing one selection updates the others/
      : /共用文件中保存的认证.*需要一起迁移：Codex, OpenCode.*同时更新全部关联后端/;
    expect(within(dialog).getAllByText(sharedMessage)).toHaveLength(2);
    for (const checkbox of checkboxes) expect(checkbox.getAttribute('aria-checked')).toBe('true');

    const start = within(dialog).getByRole('button', { name: language === 'en' ? 'Start migration' : '开始迁移' });
    await user.click(checkboxes[0]);
    for (const checkbox of checkboxes) expect(checkbox.getAttribute('aria-checked')).toBe('false');
    expect((start as HTMLButtonElement).disabled).toBe(true);

    await user.click(checkboxes[2]);
    for (const checkbox of checkboxes) expect(checkbox.getAttribute('aria-checked')).toBe('true');
    await user.click(start);
    await waitFor(() => expect(applied).toEqual([linked.map((item) => item.id)]));
  });

  it('leaves the whole linked group unchecked when any consuming row was not selected', async () => {
    serve(linked.map((item, index) => ({ ...item, selected: index !== 1 })));
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    const checkboxes = within(dialog).getAllByRole('checkbox');
    for (const checkbox of checkboxes) expect(checkbox.getAttribute('aria-checked')).toBe('false');
    const start = within(dialog).getByRole('button', { name: 'Start migration' });
    expect((start as HTMLButtonElement).disabled).toBe(true);

    await user.click(checkboxes[0]);
    await user.click(start);
    await waitFor(() => expect(applied).toEqual([linked.map((item) => item.id)]));
  });

  it('does not let a linked row it cannot carry hold back the group', async () => {
    serve([
      ...linked,
      {
        ...LEGACY,
        id: 'blocked_helper',
        proposed_action: 'keep_native',
        notes_key: 'settings.models.migration.blocked.helper',
        source_paths: ['/home/用户/.config/opencode/opencode.json'],
        required_backends: ['codex', 'opencode'],
      },
      SUBSCRIPTION,
    ]);
    renderDialog({ eligible: (item) => item.id === CODEX_KEY.id });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    expect(within(dialog).queryByRole('status')).toBeNull();
    const checkboxes = within(dialog).getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(3);
    for (const checkbox of checkboxes) expect((checkbox as HTMLButtonElement).disabled).toBe(false);
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));
    await waitFor(() => expect(applied).toEqual([linked.map((item) => item.id)]));
  });

  it('blocks a linked group whose other backend has nothing the Hub can carry', async () => {
    // The server requires every linked backend in the batch; one with no
    // carried row can never be selected, so offering the rest always fails.
    serve([
      { ...CODEX_KEY, source_paths: ['/home/用户/.profile'], required_backends: ['codex', 'opencode'] },
      {
        ...LEGACY,
        id: 'blocked_headers',
        proposed_action: 'keep_native',
        notes_key: 'settings.models.migration.blocked.headers',
        source_paths: ['/home/用户/.profile'],
        required_backends: ['codex', 'opencode'],
      },
    ]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    const [checkbox] = within(dialog).getAllByRole('checkbox');
    expect((checkbox as HTMLButtonElement).disabled).toBe(true);
    expect((within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('blocks a backend whose native config the CLI cannot parse', async () => {
    // Hub mode over an unparseable native config fails every launch, so the
    // server refuses the batch; the dialog must not offer it.
    serve([
      CODEX_KEY,
      {
        ...CODEX_KEY,
        id: 'blocked_config',
        proposed_action: 'reauth',
        selected: false,
        notes_key: 'settings.models.migration.blocked.config',
        config_blocker: true,
      },
    ]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findAllByText('OpenAI');
    const [checkbox] = within(dialog).getAllByRole('checkbox');
    expect((checkbox as HTMLButtonElement).disabled).toBe(true);
    expect((within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('names a standalone config blocker the user has to repair', async () => {
    serve([{
      ...CODEX_KEY,
      id: 'blocked_config',
      masked_detail: '',
      proposed_action: 'reauth',
      selected: false,
      notes_key: 'settings.models.migration.blocked.config',
      source_paths: ['/home/用户/.codex/config.toml'],
      config_blocker: true,
    }]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    expect((await within(dialog).findAllByText('/home/用户/.codex/config.toml')).length).toBeGreaterThan(0);
    expect(within(dialog).getByRole('status')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps a transitive three-backend closure visible and selected from one entry point', async () => {
    const items: MigrationItem[] = [SUBSCRIPTION, CODEX_KEY, LEGACY].map((item) => ({
      ...item,
      source_paths: item.backend === 'codex' ? ['/home/用户/.profile', '/home/用户/.zshrc'] : ['/home/用户/.profile'],
      required_backends: ['claude', 'codex', 'opencode'],
    }));
    serve(items);
    renderDialog({ eligible: (item) => item.backend === 'claude' });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    const checkboxes = within(dialog).getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(3);
    expect(within(dialog).getAllByText(/must migrate together: Claude Code, Codex, OpenCode/)).toHaveLength(3);
    await user.click(checkboxes[2]);
    for (const checkbox of checkboxes) expect(checkbox.getAttribute('aria-checked')).toBe('false');
    await user.click(checkboxes[1]);
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));
    await waitFor(() => expect(applied).toEqual([items.map((item) => item.id)]));
  });

  it.each([
    ['missing', undefined],
    ['empty', []],
  ] as const)('retains independent backend selection when required_backends is %s', async (_description, required) => {
    serve([CODEX_KEY, LEGACY].map((item) => ({
      ...item,
      required_backends: required ? [...required] : undefined,
    })));
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    expect(within(dialog).queryByText(/must migrate together/)).toBeNull();
    const checkboxes = within(dialog).getAllByRole('checkbox');
    await user.click(checkboxes[0]);
    expect(checkboxes[1].getAttribute('aria-checked')).toBe('true');
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));
    await waitFor(() => expect(applied).toEqual([[LEGACY.id]]));
  });
});

// ── the two entry points ────────────────────────────────────────────────
//
// Setup reuses this dialog rather than growing a second take-over, so the guard
// that matters is that Settings did not quietly change when it did: the whole
// rendered surface is asserted literally, because a copy table that resolved one
// key to the wrong scope would still render *something* and still pass a
// behaviour test.

const visibleText = (root: Element): string[] => {
  const out: string[] = [];
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walk.nextNode(); node; node = walk.nextNode()) {
    const text = node.textContent?.trim();
    if (text) out.push(text);
  }
  return out;
};

const controls = (root: Element) =>
  [...root.querySelectorAll('button')].map((button) => ({
    role: button.getAttribute('role'),
    name: button.getAttribute('aria-label') ?? button.textContent?.trim() ?? '',
    disabled: button.disabled,
  }));

describe('MigrationDialog — the Settings surface the setup scope must not disturb', () => {
  it('renders exactly the copy and controls it shipped with', async () => {
    serve([SUBSCRIPTION, CODEX_KEY, REAUTH]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');

    expect(visibleText(dialog)).toEqual([
      'Migrate to the Model Hub',
      'Import authentication saved in configuration files, shell startup files, or supported credential stores, without reading runtime environment values. After migration, Model Hub will manage the selected authentication.',
      'Claude Code',
      'Anthropic',
      'Claude 账号登录（OAuth） · Claude Code configuration',
      'Codex',
      'OpenAI',
      'sk-…9f21 · Codex configuration',
      'Later',
      'Start migration',
      'Close',
    ]);
    expect(controls(dialog)).toEqual([
      { role: 'checkbox', name: 'Anthropic · Claude 账号登录（OAuth） · Claude Code configuration', disabled: false },
      { role: 'checkbox', name: 'OpenAI · sk-…9f21 · Codex configuration', disabled: false },
      { role: null, name: 'Later', disabled: false },
      { role: null, name: 'Start migration', disabled: false },
      { role: null, name: 'Close', disabled: false },
    ]);
  });

  it('keeps owning its own scan and selection when no controlled value is passed', async () => {
    serve([CODEX_KEY]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    expect(modelsApi.scanMigration).toHaveBeenCalledTimes(1);
    expect(within(dialog).queryByText(/Only API keys can be migrated here/)).toBeNull();
  });

  it('still takes a key standing beside a subscription, because its scope is everything', async () => {
    serve([SUBSCRIPTION, CLAUDE_KEY]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/sk-ant-…1c05/);

    // No narrower scope was passed, so neither row is out of reach and the group
    // is not blocked: the whole backend migrates together, as it always has.
    expect(controls(dialog)).toEqual([
      { role: 'checkbox', name: 'Anthropic · Claude 账号登录（OAuth） · Claude Code configuration', disabled: false },
      { role: 'checkbox', name: 'Anthropic · sk-ant-…1c05 · Claude Code configuration', disabled: false },
      { role: null, name: 'Later', disabled: false },
      { role: null, name: 'Start migration', disabled: false },
      { role: null, name: 'Close', disabled: false },
    ]);
    expect(within(dialog).queryByText(/but not from here/)).toBeNull();
  });
});

const SetupHost: React.FC<{
  scan: MigrationItem[];
  /** What the caller's refreshed scan returns after each successive batch. */
  after?: MigrationItem[][];
  onClose?: () => void;
  onApplied?: (applied: number) => void;
  /** What a host that can withdraw permission mid-review passes. Settings passes
   *  nothing at all, which is the default and what every case above relies on. */
  writable?: boolean;
}> = ({ scan, after = [], onClose = () => {}, onApplied, writable }) => {
  const [selection, setSelection] = useState<MigrationSelection>({
    scan: { items: scan },
    selectedBackends: [],
  });
  const [round, setRound] = useState(0);
  return (
    <MigrationDialog
      open
      scope="setup"
      eligible={isImportableKey}
      takeable={isImportableKey}
      value={selection}
      onChange={setSelection}
      writable={writable}
      onApplied={(applied) => {
        onApplied?.(applied);
        // The receipt is a refresh trigger, not a success receipt: re-read and
        // let the refreshed scan decide what is left.
        setSelection({ scan: { items: after[round] ?? [] }, selectedBackends: [] });
        setRound((prior) => prior + 1);
      }}
      onClose={onClose}
    />
  );
};

const renderSetup = (props: React.ComponentProps<typeof SetupHost>) => {
  const view = render(
    <I18nextProvider i18n={i18n}>
      <SetupHost {...props} />
    </I18nextProvider>,
  );
  /** The host changing its half of the contract without remounting the dialog. */
  const show = (next: Partial<React.ComponentProps<typeof SetupHost>>) => view.rerender(
    <I18nextProvider i18n={i18n}>
      <SetupHost {...props} {...next} />
    </I18nextProvider>,
  );
  return { ...view, show };
};

describe('MigrationDialog — the setup scope', () => {
  it('wears setup copy and never scans for a controlled caller', async () => {
    const scan = vi.spyOn(modelsApi, 'scanMigration');
    renderSetup({ scan: [CODEX_KEY, LEGACY] });

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Migrate to Model Hub')).toBeTruthy();
    expect(within(dialog).getByText(/After migration, these assistants connect through Model Hub/)).toBeTruthy();
    expect(within(dialog).getByText(/Only API keys can be migrated here/)).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: 'Not now' })).toBeTruthy();
    expect(scan).not.toHaveBeenCalled();
  });

  it('starts from the caller selection and submits one atomic batch', async () => {
    serve();
    renderSetup({ scan: [CODEX_KEY, LEGACY] });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement;
    // Nothing is consented to until the caller says so, whatever the scan
    // pre-selected on its rows.
    expect(confirm.disabled).toBe(true);

    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(applied).toEqual([[CODEX_KEY.id]]));
  });

  it('reports what landed, offers the remainder, and re-enters selection with it', async () => {
    serve();
    renderSetup({ scan: [CODEX_KEY, LEGACY], after: [[LEGACY], []] });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await within(dialog).findByText('Migrated 1 configuration item');
    expect(within(dialog).getByText('1 item needs review. Continue any time.')).toBeTruthy();
    // The dialog stays: setup has more to say than "it closed".
    expect(within(dialog).queryByRole('button', { name: 'Start migration' })).toBeNull();

    await user.click(within(dialog).getByRole('button', { name: 'Review remaining' }));
    await within(dialog).findByRole('button', { name: 'Start migration' });
    expect(within(dialog).queryByRole('checkbox', { name: /sk-…9f21/ })).toBeNull();
    expect(within(dialog).getByRole('checkbox', { name: /自建中转/ })).toBeTruthy();
  });

  it('counts every batch of one session, not only the last', async () => {
    serve();
    renderSetup({ scan: [CODEX_KEY, LEGACY], after: [[LEGACY], []] });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));
    await within(dialog).findByText('Migrated 1 configuration item');
    await user.click(within(dialog).getByRole('button', { name: 'Review remaining' }));

    await user.click(within(dialog).getByRole('checkbox', { name: /自建中转/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await within(dialog).findByText('Migrated 2 configuration items');
    expect(within(dialog).queryByText(/needs review/)).toBeNull();
    expect(applied).toEqual([[CODEX_KEY.id], [LEGACY.id]]);
  });

  it('returns to the same selection after a failed batch', async () => {
    vi.spyOn(modelsApi, 'applyMigration').mockRejectedValue(
      new ApiCallError('migration_native_busy', 'busy'),
    );
    renderSetup({ scan: [CODEX_KEY, LEGACY] });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(showToast).toHaveBeenCalled());
    const confirm = await within(dialog).findByRole('button', { name: 'Start migration' });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    expect(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }).getAttribute('aria-checked')).toBe('true');
    expect(within(dialog).queryByText(/Migrated/)).toBeNull();
  });

  it('cannot be dismissed while a batch is in flight', async () => {
    let release: (value: { applied: number; sources: [] }) => void = () => {};
    vi.spyOn(modelsApi, 'applyMigration').mockImplementation(
      () => new Promise((resolve) => { release = resolve; }),
    );
    const onClose = vi.fn();
    renderSetup({ scan: [CODEX_KEY], onClose });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));
    await within(dialog).findByText('Migrating 1 configuration item…');

    await user.click(within(dialog).getByRole('button', { name: 'Close' }));
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();

    release({ applied: 1, sources: [] });
    await within(dialog).findByText('Migrated 1 configuration item');
    await user.click(within(dialog).getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalled();
  });

  it('refuses a batch its host no longer admits, and keeps the review readable', async () => {
    serve();
    const onClose = vi.fn();
    const { show } = renderSetup({ scan: [CODEX_KEY, LEGACY], onClose, writable: true });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }));
    const confirm = () => within(dialog).getByRole<HTMLButtonElement>('button', { name: 'Start migration' });
    expect(confirm().disabled).toBe(false);

    // The host stopped admitting a write — for setup, the engine these keys would be
    // migrated INTO. Only the batch is refused: what was chosen is still chosen, still
    // readable, and still leavable, because closing this is a different answer.
    show({ writable: false });
    expect(confirm().disabled).toBe(true);
    await user.click(confirm());
    expect(modelsApi.applyMigration).not.toHaveBeenCalled();
    expect(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }).getAttribute('aria-checked')).toBe('true');
    expect(within(dialog).getByText(/sk-…9f21/)).toBeTruthy();
    expect(within(dialog).queryByText(/Migrated/)).toBeNull();

    await user.click(within(dialog).getByRole('button', { name: 'Not now' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('settles a batch it had already sent when permission goes away mid-flight', async () => {
    let release: (value: { applied: number; sources: [] }) => void = () => {};
    const apply = vi.spyOn(modelsApi, 'applyMigration').mockImplementation(
      () => new Promise((resolve) => { release = resolve; }),
    );
    const onApplied = vi.fn();
    const { show } = renderSetup({ scan: [CODEX_KEY], onApplied, writable: true });
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…9f21/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));
    await within(dialog).findByText('Migrating 1 configuration item…');

    // Withdrawn while that batch is on the server's side of the wire. It owns its own
    // outcome: permission governs the next write, and a report dropped here would
    // leave keys migrated with nothing on screen saying so.
    show({ writable: false });
    release({ applied: 1, sources: [] });

    await within(dialog).findByText('Migrated 1 configuration item');
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(onApplied).toHaveBeenCalledWith(1);
    expect(apply).toHaveBeenCalledTimes(1);
    // The report is the whole of what is offered: there is no second batch to send.
    expect(within(dialog).queryByRole('button', { name: 'Start migration' })).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: 'Done' }));
  });

  it('shows a key it cannot take here, and says why rather than offering it', async () => {
    renderSetup({ scan: [SUBSCRIPTION, CLAUDE_KEY] });

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/sk-ant-…1c05/);

    // The key stays on screen — a detected credential the screen dropped would be
    // unexplained — but its group is blocked, because taking it would take the
    // subscription beside it, which setup has no consent for.
    expect(controls(dialog)).toEqual([
      { role: 'checkbox', name: 'Anthropic · Claude 账号登录（OAuth） · Claude Code configuration', disabled: true },
      { role: 'checkbox', name: 'Anthropic · sk-ant-…1c05 · Claude Code configuration', disabled: true },
      { role: null, name: 'Not now', disabled: false },
      { role: null, name: 'Start migration', disabled: true },
      { role: null, name: 'Close', disabled: false },
    ]);
    // Not the server's "cannot be imported": it can be, just not from here.
    expect(
      within(dialog).getByText('This credential can be migrated, but not from here. Review it in Settings.'),
    ).toBeTruthy();
    expect(within(dialog).queryByText(/cannot be imported/)).toBeNull();
  });
});
