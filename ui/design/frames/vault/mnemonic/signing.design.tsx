// How a signature request names the wallet account. Standard tier: the shipped approval card adds an
// Account row (the account as it was when the Agent asked, with its path) and signs as that account's
// address. Protected tier: the sandbox window adds the account number and path from authenticated
// metadata (mirrored here in product tokens; the real one is vault-sandbox design/signing.html, after 3.2.6).
// A protected wallet's Legacy or SegWit request: the page's card names the account, the type it signs
// as and that type's path; the sandbox window then shows the transaction. A browser card would start
// the vault sandbox, which no frame may load, so these are drawn as the desktop app shows them (the
// same card, finished in the browser); the browser's card differs only in its Approve button.
import type { BitcoinAddressType, VaultRequest } from '@/context/ApiContext';
import { VaultApprovalCard } from '@/components/ui/vault-approval-card';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { SandboxSignMirror, SandboxWordsMirror } from '../../../kit/vault-mnemonic/signing';
import { frames } from '../../../frame';
import { consolePage, phoneShell } from '../../workbench/fixtures';
import { signRequest, vaultApi, vaultFixtures, vaultSecrets } from '../fixtures';
import { walletAccounts, walletSecret } from './fixtures';

const account = walletAccounts[1];
const hdSign = {
  ...signRequest,
  id: 'vrq_hd_sign',
  secret_name: 'AGENT_WALLET',
  delivery: { digest: '5f0b6e2a91c4d7e83b1a26f4c09d8e7712a3b4c5d6e7f8091a2b3c4d5e6f7081', scheme: 'ecdsa-secp256k1-recoverable', derivation: { chain: 'evm', account: 1 } },
  card: { ...(signRequest.card as Record<string, unknown>), secret_name: 'AGENT_WALLET', derivation: { chain: 'evm', account: 1 }, account },
} as unknown as VaultRequest;
const fixtures = vaultFixtures(vaultApi({ secrets: [...vaultSecrets, walletSecret()], grants: [], requests: [hdSign] }));
const review = ['role=button[name="{t:vaults.requests.review}"]'];

const BTC = {
  p2pkh: { address: 'btc_legacy', path: "m/44'/0'/0'/0/1", algorithm: 'bitcoin-legacy-sighash-all' },
  p2wpkh: { address: 'btc_segwit', path: "m/84'/0'/0'/0/1", algorithm: 'bitcoin-bip143-sighash-all' },
} as const;
const protectedBtcSign = (type: 'p2pkh' | 'p2wpkh'): VaultRequest => {
  const derivation = { chain: 'bitcoin', account: 1, address_type: type as BitcoinAddressType };
  const digest = '9c2e4b7a1f30d58e6a4b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f';
  return {
    ...signRequest,
    id: `vrq_hd_${type}`,
    secret_name: 'COLD_WALLET',
    delivery: {
      digest, scheme: 'ecdsa-secp256k1-der', derivation,
      signing_context: { kind: `bitcoin-${type}-v1`, addressType: type, network: 'bitcoin', sighashType: 1, digestAlgorithm: BTC[type].algorithm, digest },
    },
    card: {
      ...(signRequest.card as Record<string, unknown>),
      secret_name: 'COLD_WALLET',
      protection: 'protected',
      derivation,
      account: { ...account, always_ask: true, bitcoin_address_type: type, bitcoin_address: account[BTC[type].address], bitcoin_path: BTC[type].path },
    },
  } as unknown as VaultRequest;
};
const desktopFixtures = vaultFixtures(vaultApi({ secrets: vaultSecrets, grants: [], requests: [] }), { desktopShell: true });
const card = (type: 'p2pkh' | 'p2wpkh') => () => (
  <div className="p-6"><VaultApprovalCard request={protectedBtcSign(type)} onResolved={() => {}} onCancel={() => {}} /></div>
);

const designFrames = frames([
  { name: 'standard', title: '标准档 · 审批卡写明账户 #1、链和派生路径', width: 1192, height: 960, status: 'shipped', route: '/vaults', fixtures, click: review, render: consolePage(<VaultsPage />) },
  { name: 'standard-phone', title: '标准档 · 审批 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: review, render: phoneShell('/vaults', <VaultsPage />) },
  { name: 'protected-legacy', title: '保护档 · 传统（Legacy）BTC 签名请求：账户 #1、BTC · 传统、m/44′ 路径', width: 640, height: 480, status: 'shipped', fixtures: desktopFixtures, render: card('p2pkh') },
  { name: 'protected-segwit', title: '保护档 · SegWit BTC 签名请求：账户 #1、BTC · SegWit、m/84′ 路径', width: 640, height: 480, status: 'shipped', fixtures: desktopFixtures, render: card('p2wpkh') },
  { name: 'protected', title: '保护档 · 沙箱窗口新增「钱包账户」一行（镜像，后续）', width: 520, height: 760, status: 'approved', fixtures, render: () => <SandboxSignMirror /> },
  { name: 'protected-create-window', title: '保护档 · 安全窗口里显示助记词（镜像，后续）', width: 520, height: 820, status: 'approved', fixtures, render: () => <SandboxWordsMirror mode="create" /> },
  { name: 'protected-import-window', title: '保护档 · 安全窗口里输入并核对（镜像，后续）', width: 520, height: 860, status: 'approved', fixtures, render: () => <SandboxWordsMirror mode="import" /> },
  { name: 'protected-phone', title: '保护档 · 沙箱窗口 · 手机（镜像，后续）', width: 390, height: 844, status: 'approved', fixtures, render: () => <SandboxSignMirror /> },
]);

export default designFrames;
