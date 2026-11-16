// The one add / provide / edit dialog every entry point opens (the Vaults page's Add, a pending provision
// row, the `$<NAME>` chat card, the chat provision card): Add and Provide are drawn with the props those
// callers pass, Edit as the row menu opens it on the page.
import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import { VaultsPage } from '@/components/workbench/VaultsPage';
import { ProtectedHdDesignState } from '@/lib/protectedHdDesign';

import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { provisionRequest, secret, vaultApi, vaultFixtures, vaultSecrets } from './fixtures';

const noop = () => {};
const fixtures = vaultFixtures();
const quiet = vaultFixtures(vaultApi({ grants: [], requests: [] }));
// The same list, with ask-before-signing already on for the standard signing key.
const askOn = vaultFixtures(vaultApi({
  grants: [],
  requests: [],
  secrets: vaultSecrets.map((s) => (s.name === 'ETH_HOT_KEY' ? { ...s, policy: { ...s.policy, always_ask: true } } : s)),
}));
const editKey = ['li:has-text("ETH_HOT_KEY") >> role=button[name="{t:vaults.rowActions}"]', 'role=menuitem[name="{t:vaults.editSecret}"]'];
const askSwitch = 'role=switch[name="{t:vaults.dialog.alwaysAskSign}"]';

const create = () => <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />;
const provide = () => (
  <VaultSecretDialog open onOpenChange={noop} request={provisionRequest} onCancel={noop} onDeny={noop} onCreated={noop} />
);
const page = consolePage(<VaultsPage />);
// Rows sort by name: AWS_DEPLOY, ETH_HOT_KEY, GITHUB_TOKEN, OPENAI_API_KEY, STRIPE_LIVE_KEY.
const editRow = (nth: number) => [`role=button[name="{t:vaults.rowActions}"] >> nth=${nth}`, 'role=menuitem[name="{t:vaults.editSecret}"]'];

// Protected static secrets: values are typed only in the secure window, opened by Save (contract
// "seal a whole Protected secret with values typed in the secure window" v1.1). The design state draws
// a locked vault with the window ready; the window itself is the sandbox's own frames.
const protectedState = { phase: { kind: 'idle' as const }, name: 'GITHUB_LOGIN' };
const protectedCreate = () => (
  <ProtectedHdDesignState.Provider value={protectedState}>
    <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />
  </ProtectedHdDesignState.Provider>
);
const protectedTier = 'role=button[name^="{t:vaults.dialog.protectedProtection}"]';
const loginTemplate = ['role=button[name="{t:vaults.fields.useTemplate}"]', 'role=menuitem[name="{t:vaults.fields.templateLogin2fa}"]'];
const protectedLogin = vaultFixtures(vaultApi({
  secrets: [secret('GITHUB_LOGIN', { protection: 'protected', fields: ['password', 'username', 'totp'], default_field: 'password', description: '工作用 GitHub 账号', tags: ['dev'] })],
  grants: [],
  requests: [],
}));
const protectedPage = consolePage(
  <ProtectedHdDesignState.Provider value={protectedState}>
    <VaultsPage />
  </ProtectedHdDesignState.Provider>,
);

const designFrames = frames([
  {
    name: 'create',
    title: '添加密钥 · 静态值（标准档）',
    width: 1000,
    height: 1000,
    status: 'shipped',
    fixtures,
    render: create,
  },
  {
    name: 'tags',
    title: '添加密钥 · 选择或新建标签',
    width: 1000,
    height: 1000,
    status: 'shipped',
    fixtures,
    click: ['role=combobox[name="{t:vaults.dialog.tags}"]'],
    render: create,
  },
  {
    name: 'advanced',
    title: '添加密钥 · 高级（代理域名与鉴权方式）',
    width: 1000,
    height: 1200,
    status: 'shipped',
    fixtures,
    click: ['role=button[name="{t:vaults.dialog.advanced}"]'],
    render: create,
  },
  {
    name: 'keypair',
    title: '添加密钥 · 签名密钥 · 生成（创建时生成，创建后显示地址）',
    width: 1000,
    height: 1100,
    status: 'shipped',
    fixtures,
    click: ['role=radio[name="{t:vaults.dialog.kindKeypair}"]'],
    render: create,
  },
  {
    name: 'keypair-import',
    title: '添加密钥 · 签名密钥 · 导入',
    width: 1000,
    height: 1100,
    status: 'shipped',
    fixtures,
    click: ['role=radio[name="{t:vaults.dialog.kindKeypair}"]', 'role=radio[name="{t:vaults.dialog.signingImport}"]'],
    render: create,
  },
  {
    name: 'keypair-phone',
    title: '添加密钥 · 签名密钥 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures,
    click: ['role=radio[name="{t:vaults.dialog.kindKeypair}"]'],
    render: create,
  },
  {
    name: 'provide',
    title: 'Agent 需要一个密钥 · 填写 $ANTHROPIC_API_KEY',
    width: 1000,
    height: 940,
    status: 'shipped',
    fixtures,
    render: provide,
  },
  {
    name: 'edit',
    title: '编辑密钥 · 描述、标签与代理策略',
    width: 1192,
    height: 1040,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    click: editRow(2),
    render: page,
  },
  {
    name: 'edit-keypair',
    title: '编辑签名密钥 · 可以打开「每次签名都要我确认」',
    width: 1192,
    height: 940,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    click: editKey,
    render: page,
  },
  {
    name: 'edit-keypair-ask-on',
    title: '编辑签名密钥 · 打开后提示：保存后暂时不能关闭',
    width: 1192,
    height: 940,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    click: [...editKey, askSwitch],
    render: page,
  },
  {
    name: 'edit-keypair-ask-locked',
    title: '编辑签名密钥 · 已打开，暂时不能关闭',
    width: 1192,
    height: 940,
    status: 'shipped',
    route: '/vaults',
    fixtures: askOn,
    click: editKey,
    render: page,
  },
  {
    name: 'edit-fields',
    title: '编辑密钥 · 多字段密钥',
    width: 1192,
    height: 940,
    status: 'shipped',
    route: '/vaults',
    fixtures: quiet,
    click: editRow(0),
    render: page,
  },
  {
    name: 'create-phone',
    title: '添加密钥 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures,
    render: create,
  },
  {
    name: 'protected-window',
    title: '添加密钥 · 保护档 · 值在安全窗口里填写（页面上没有输入框）',
    width: 1000,
    height: 1100,
    status: 'shipped',
    fixtures,
    click: [protectedTier],
    render: protectedCreate,
  },
  {
    name: 'protected-window-fields',
    title: '添加密钥 · 保护档 · 多字段：页面只填字段名，值都在安全窗口里',
    width: 1000,
    height: 1240,
    status: 'shipped',
    fixtures,
    click: [protectedTier, ...loginTemplate],
    render: protectedCreate,
  },
  {
    name: 'protected-window-phone',
    title: '添加密钥 · 保护档 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures,
    click: [protectedTier],
    render: protectedCreate,
  },
  {
    name: 'edit-protected',
    title: '编辑保护档密钥 · 各字段「已保存」，值不在页面上',
    width: 1192,
    height: 1000,
    status: 'shipped',
    route: '/vaults',
    fixtures: protectedLogin,
    click: editRow(0),
    render: protectedPage,
  },
  {
    name: 'edit-protected-change',
    title: '编辑保护档密钥 · 点「在安全窗口里修改值」：保存时打开安全窗口',
    width: 1192,
    height: 1000,
    status: 'shipped',
    route: '/vaults',
    fixtures: protectedLogin,
    click: [...editRow(0), 'role=button[name="{t:vaults.dialog.window.changeValues}"]'],
    render: protectedPage,
  },
  {
    name: 'edit-protected-add',
    title: '编辑保护档密钥 · 新增字段：名称在页面，值在安全窗口里',
    width: 1192,
    height: 1040,
    status: 'shipped',
    route: '/vaults',
    fixtures: protectedLogin,
    click: [...editRow(0), 'role=button[name="{t:vaults.fields.add}"]'],
    render: protectedPage,
  },
  {
    name: 'provide-phone',
    title: 'Agent 需要一个密钥 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures,
    render: provide,
  },
]);

export default designFrames;
