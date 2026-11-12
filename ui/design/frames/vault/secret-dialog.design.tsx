// The one add / provide / edit dialog every entry point opens (the Vaults page's Add, a pending provision
// row, the `$<NAME>` chat card, the chat provision card): Add and Provide are drawn with the props those
// callers pass, Edit as the row menu opens it on the page.
import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { provisionRequest, vaultApi, vaultFixtures } from './fixtures';

const noop = () => {};
const fixtures = vaultFixtures();
const quiet = vaultFixtures(vaultApi({ grants: [], requests: [] }));

const create = () => <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />;
const provide = () => (
  <VaultSecretDialog open onOpenChange={noop} request={provisionRequest} onCancel={noop} onDeny={noop} onCreated={noop} />
);
const page = consolePage(<VaultsPage />);
// Rows sort by name: AWS_DEPLOY, ETH_HOT_KEY, GITHUB_TOKEN, OPENAI_API_KEY, STRIPE_LIVE_KEY.
const editRow = (nth: number) => [`role=button[name="{t:vaults.rowActions}"] >> nth=${nth}`, 'role=menuitem[name="{t:vaults.editSecret}"]'];

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
