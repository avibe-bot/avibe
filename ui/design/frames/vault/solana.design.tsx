// A standard-tier Solana signing key in the Vault (docs/plans/vault-solana.md §8): creating one in the
// Add dialog, its row in the list, and approving a signature over exact message bytes. avault creates
// the key, so the dialog shows no address before saving. The key is the RFC 8032 test-1 public key and
// the recipient its test-2 key: published vectors, never a wallet. Protected Solana keys and their
// approval window live in the sandbox. On the protected tier the dialog first sets up or unlocks the
// protected vault, as for any protected key; the frames draw the key builder that follows.
import { VaultProtectedKeyFields } from '@/components/ui/vault-protected-key-fields';
import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import { VaultSolanaKeyFields } from '@/components/ui/vault-solana-key-fields';
import { VaultsPage } from '@/components/workbench/VaultsPage';
import type { VaultRequest } from '@/context/ApiContext';

import { frames } from '../../frame';
import { consolePage, phoneShell } from '../workbench/fixtures';
import { secret, signRequest, vaultApi, vaultFixtures } from './fixtures';

const noop = () => {};
const address = 'FVen3X669xLzsi6N2V91DoiyzHzg1uAgqiT8jZ9nS96Z';

// A legacy message with one System transfer of 0.01 SOL: 150 bytes.
const transfer =
  '01000103d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'
  + '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c'
  + '0000000000000000000000000000000000000000000000000000000000000000'
  + 'c3a31d0d0fb5cc1047ecf3c8316bb8fdaadee46efcc68d5848a3f6644f597f44'
  + '01020200010c020000008096980000000000';
// 1,180 patterned bytes, near the most one signer's message can hold: the box scrolls, the card does not grow.
const long = Array.from({ length: 1180 }, (_, index) => ((index * 73 + 41) & 0xff).toString(16).padStart(2, '0')).join('');

const solanaKey = secret('SOL_DEVNET_KEY', {
  kind: 'keypair',
  signer_kind: 'local',
  description: 'Devnet 自动化（公开的测试向量，切勿充值）',
  tags: ['crypto'],
  signing_curve: 'ed25519',
  signing_addresses: { solana: address },
  policy: { always_ask: true },
});
const ethKey = secret('ETH_HOT_KEY', {
  kind: 'keypair',
  signer_kind: 'local',
  description: '测试网合约部署（公开的合成测试密钥，切勿充值）',
  tags: ['crypto'],
  signing_curve: 'secp256k1',
  signing_addresses: { eth: '0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf' },
});

// An agent asks a standard Solana key that asks before every signature to sign a message.
const sign = (messageHex: string): VaultRequest => ({
  ...signRequest,
  id: 'vrq_solana',
  secret_name: solanaKey.name,
  card: { ...signRequest.card, secret_name: solanaKey.name },
  session: { id: 'ses_airdrop', title: 'Devnet 空投脚本', label: null, platform: 'avibe', scope_kind: 'project', is_workbench: true },
  delivery: { scheme: 'ed25519', message_hex: messageHex },
});

// An avault new enough to create Solana keys.
const dependencies = { '/api/dependencies': { deps: [{ id: 'avault', kind: 'tool', required: true, installed: true, version: '0.1.10', status: 'ready' }] } };
const api = (requests: VaultRequest[] = []) => ({ ...vaultApi({ secrets: [ethKey, solanaKey], grants: [], requests }), ...dependencies });
const listed = vaultFixtures(api());
const pending = (messageHex: string) => vaultFixtures(api([sign(messageHex)]));

const dialog = () => <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />;
const page = consolePage(<VaultsPage />);
const phonePage = phoneShell('/vaults', <VaultsPage />);

const solana = ['role=radio[name="{t:vaults.dialog.kindKeypair}"]', 'role=radio[name="{t:vaults.dialog.chainSolana}"]'];
const importKey = [...solana, 'role=radio[name="{t:vaults.dialog.signingImport}"]'];
const review = ['role=button[name="{t:vaults.requests.review}"]'];
const details = [...review, 'role=button[name="{t:vaults.approval.details}"]'];

const designFrames = frames([
  {
    name: 'create-generate',
    title: '添加密钥 · Solana 签名密钥 · 生成',
    width: 1000,
    height: 1240,
    status: 'shipped',
    fixtures: listed,
    click: solana,
    render: dialog,
  },
  {
    name: 'create-import',
    title: '添加密钥 · Solana 签名密钥 · 导入',
    width: 1000,
    height: 1320,
    status: 'shipped',
    fixtures: listed,
    click: importKey,
    render: dialog,
  },
  {
    name: 'create-import-failed',
    title: '添加密钥 · Solana 导入 · avault 没有导入粘贴的内容',
    width: 512,
    height: 330,
    status: 'shipped',
    fixtures: listed,
    // The dialog reaches this state only after a paste and a failed save, so the builder is drawn
    // on its own, at the width the dialog gives it.
    render: () => (
      <div className="p-6">
        <VaultSolanaKeyFields source="import" onSourceChange={noop} importText="[12, 34, 56]" onImportTextChange={noop} importFailed />
      </div>
    ),
  },
  {
    name: 'create-protected',
    title: '添加密钥 · 保护档 Solana 密钥 · 创建时在沙箱内生成',
    width: 512,
    height: 120,
    status: 'shipped',
    fixtures: listed,
    // The dialog reaches this builder only once the protected vault is unlocked in the sandbox, so
    // the builder is drawn on its own, at the width the dialog gives it.
    render: () => (
      <div className="p-6">
        <VaultProtectedKeyFields chain="solana" />
      </div>
    ),
  },
  {
    name: 'create-generate-phone',
    title: '添加密钥 · Solana · 生成 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: listed,
    click: solana,
    render: dialog,
  },
  {
    name: 'create-import-phone',
    title: '添加密钥 · Solana · 导入 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: listed,
    click: importKey,
    render: dialog,
  },
  {
    name: 'address',
    title: '密钥列表 · Solana 密钥只有一行 Solana 地址',
    width: 1192,
    height: 620,
    status: 'shipped',
    route: '/vaults',
    fixtures: listed,
    render: page,
  },
  {
    name: 'address-phone',
    title: '密钥列表 · Solana 地址 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures: listed,
    render: phonePage,
  },
  {
    name: 'approval',
    title: '审批 · 标准档 Solana 密钥签名 · 摘要（详情收起）',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(transfer),
    click: review,
    render: page,
  },
  {
    name: 'approval-details',
    title: '审批 · 详情展开：完整地址与完整待签名字节',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(transfer),
    click: details,
    render: page,
  },
  {
    name: 'approval-details-long',
    title: '审批 · 长消息在详情框内滚动',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(long),
    click: details,
    render: page,
  },
  {
    name: 'approval-empty',
    title: '审批 · 空消息也是合法消息（详情展开）',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(''),
    click: details,
    render: page,
  },
  {
    name: 'approval-phone',
    title: '审批 · 摘要（详情收起）· 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(transfer),
    click: review,
    render: phonePage,
  },
  {
    name: 'approval-details-phone',
    title: '审批 · 详情展开 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(transfer),
    click: details,
    render: phonePage,
  },
]);

export default designFrames;
