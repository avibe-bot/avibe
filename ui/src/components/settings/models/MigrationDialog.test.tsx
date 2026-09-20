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
    expect(within(dialog).getByText('This credential cannot be imported. Use the existing Add flow in Model Hub to add this source manually.')).toBeTruthy();
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

  it.each([
    ['settings.models.migration.blocked.config', "Check this CLI's saved configuration, then scan again."],
    ['settings.models.migration.blocked.environment', 'This environment-based authentication cannot be imported. Save the key in a supported configuration file, then scan again.'],
    ['settings.models.migration.blocked.credential', 'This credential could not be read or is not supported. Check credential access and the saved authentication format, then scan again.'],
    ['settings.models.migration.blocked.unknown', 'This credential cannot be imported. Use the existing Add flow in Model Hub to add this source manually.'],
  ] as const)('maps blocked note %s to actionable copy', async (notes_key, message) => {
    const blocked = { ...REAUTH, notes_key };
    serve([CODEX_KEY, blocked]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('OpenAI')).toBeTruthy());
    expect((within(dialog).getByRole('status') as HTMLElement).textContent).toContain(message);
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

  const blockedReasons = [
    {
      reason: 'dynamic_shell',
      path: '/home/用户/.zshrc',
      en: /expressions or commands.*literal values.*never executes shell code/,
      zh: /表达式或命令.*固定值.*不会执行 Shell 代码/,
    },
    {
      reason: 'ambiguous_shell',
      path: '/home/用户/.profile',
      en: /Conflicting saved assignments.*listed files.*unambiguous literal value/,
      zh: /列出的文件.*互相冲突.*固定值.*统一相关配置/,
    },
    {
      reason: 'unreadable',
      path: '/home/用户/.bashrc',
      en: /source file could not be read.*Avibe has read access/,
      zh: /无法读取此配置文件.*Avibe 是否有读取权限/,
    },
    {
      reason: 'reference',
      path: '/home/用户/.claude/settings.json',
      en: /referenced variable.*no unambiguous saved literal value.*save.*key.*listed configuration or shell startup file/,
      zh: /引用的变量.*固定值.*列出的配置文件或 Shell 启动文件.*保存对应的密钥/,
    },
    {
      reason: 'helper',
      path: '/home/用户/.claude/settings.local.json',
      en: /supplied by a command.*supported static configuration.*add the source manually/,
      zh: /由命令提供.*静态配置.*手动添加供应商/,
    },
    {
      reason: 'token',
      path: '/home/用户/.zshenv',
      en: /login token or authentication format cannot be imported.*Add a source.*sign in again/,
      zh: /登录令牌或认证格式暂不支持导入.*添加供应商.*重新登录/,
    },
    {
      reason: 'headers',
      path: '/home/用户/.zprofile',
      en: /Custom credential headers cannot be imported.*supported authentication method/,
      zh: /自定义认证请求头.*受支持的认证方式/,
    },
    {
      reason: 'transport',
      path: '/home/用户/.bashrc',
      en: /cannot send.*saved authentication in the same way.*original files are unchanged.*permissions will not fix/,
      zh: /无法按此接口原有的方式发送认证.*原配置文件未改动.*修改文件权限不能解决/,
    },
  ] as const;

  it.each(['en', 'zh'] as const)('shows every blocked file and its specific %s remedy while preserving backend selection', async (language) => {
    await i18n.changeLanguage(language);
    serve([
      ...blockedReasons.map(({ reason, path }) => ({
        ...REAUTH,
        id: `blocked_${reason}`,
        notes_key: `settings.models.migration.blocked.${reason}`,
        source_paths: [path],
      })),
      SUBSCRIPTION,
      { ...CODEX_KEY, source_paths: ['/home/用户/.codex/auth.json'] },
    ]);
    renderDialog();
    const user = userEvent.setup();

    const dialog = await screen.findByRole('dialog');
    const status = await within(dialog).findByRole('status');
    const warnings = within(status).getAllByRole('listitem');
    expect(warnings).toHaveLength(blockedReasons.length);
    for (const { path, ...reason } of blockedReasons) {
      const warning = warnings.find((element) => (
        within(element).queryByText(path) && within(element).queryByText(reason[language])
      ));
      expect(warning).toBeTruthy();
      expect(within(warning!).getByText(reason[language])).toBeTruthy();
    }
    expect(status.textContent).not.toMatch(/Allow credential access|请允许访问凭据|Remove this CLI|清除.*环境变量/);
    for (const row of within(dialog).getAllByRole('checkbox', { name: /sk-ant-…4b7e|Claude 账号登录/ })) {
      expect((row as HTMLButtonElement).disabled).toBe(true);
      expect(row.getAttribute('aria-checked')).toBe('false');
    }
    const supportedRow = within(dialog).getByRole('checkbox', { name: /OpenAI.*\/home\/用户\/\.codex\/auth\.json/ });
    expect((supportedRow as HTMLButtonElement).disabled).toBe(false);
    expect(supportedRow.getAttribute('aria-checked')).toBe('true');
    await user.click(within(dialog).getByRole('button', { name: language === 'en' ? 'Start migration' : '开始迁移' }));
    await waitFor(() => expect(applied).toEqual([[CODEX_KEY.id]]));
  });

  it('deduplicates only matching source/reason pairs, keeping different files and reasons', async () => {
    const dynamic: MigrationItem = {
      ...REAUTH,
      notes_key: 'settings.models.migration.blocked.dynamic_shell',
      source_paths: ['/home/用户/.zshrc', '/home/用户/.profile', '/home/用户/.zshrc'],
    };
    serve([
      dynamic,
      { ...dynamic, id: 'duplicate', source_paths: ['/home/用户/.zshrc'] },
      { ...dynamic, id: 'different_reason', notes_key: 'settings.models.migration.blocked.reference', source_paths: ['/home/用户/.zshrc'] },
    ]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    const status = await within(dialog).findByRole('status');
    const warnings = within(status).getAllByRole('listitem');
    expect(warnings).toHaveLength(3);
    expect(within(status).getAllByText('/home/用户/.zshrc')).toHaveLength(2);
    expect(within(status).getAllByText('/home/用户/.profile')).toHaveLength(1);
    expect(within(status).getAllByText(/expressions or commands/)).toHaveLength(2);
    expect(within(status).getAllByText(/referenced variable/)).toHaveLength(1);
    expect((within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps distinct blocked legacy rows visible without inventing file locators', async () => {
    serve([
      { ...REAUTH, notes_key: 'settings.models.migration.blocked.credential' },
      { ...REAUTH, id: 'legacy_second', masked_detail: 'sk-ant-…5678', notes_key: 'settings.models.migration.blocked.environment' },
    ]);
    renderDialog();

    const dialog = await screen.findByRole('dialog');
    const status = await within(dialog).findByRole('status');
    const warnings = within(status).getAllByRole('listitem');
    expect(warnings).toHaveLength(2);
    expect(within(warnings[0]).getByText('Claude Code configuration')).toBeTruthy();
    expect(within(warnings[0]).getByText(/could not be read or is not supported/)).toBeTruthy();
    expect(within(warnings[1]).getByText('Claude Code configuration')).toBeTruthy();
    expect(within(warnings[1]).getByText(/Save the key in a supported configuration file/)).toBeTruthy();
    expect(status.textContent).not.toMatch(/\.json|\.zshrc|\.profile/);
  });

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

  it('shows a linked backend blocker in each affected group, even outside the entry-point scope', async () => {
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

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('OpenAI');
    const warnings = within(dialog).getAllByRole('status');
    expect(warnings).toHaveLength(2);
    for (const warning of warnings) {
      expect(within(warning).getByText('/home/用户/.config/opencode/opencode.json')).toBeTruthy();
      expect(within(warning).getByText(/Credentials supplied by a command cannot be imported/)).toBeTruthy();
    }
    const checkboxes = within(dialog).getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(4);
    for (const checkbox of checkboxes) {
      expect((checkbox as HTMLButtonElement).disabled).toBe(true);
      expect(checkbox.getAttribute('aria-checked')).toBe('false');
    }
    expect((within(dialog).getByRole('button', { name: 'Start migration' }) as HTMLButtonElement).disabled).toBe(true);
    expect(modelsApi.applyMigration).not.toHaveBeenCalled();
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
