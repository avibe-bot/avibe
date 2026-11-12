// How a signature request names the wallet account. Standard tier: the shipped approval card adds an
// Account row (the account as it was when the Agent asked, with its path) and signs as that account's
// address. Protected tier: the sandbox window adds the account number and path from authenticated
// metadata (mirrored here in product tokens; the real one is vault-sandbox design/signing.html, after 3.2.6).
import type { VaultRequest } from '@/context/ApiContext';
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

const designFrames = frames([
  { name: 'standard', title: '标准档 · 审批卡写明账户 #1、链和派生路径', width: 1192, height: 960, status: 'shipped', route: '/vaults', fixtures, click: review, render: consolePage(<VaultsPage />) },
  { name: 'standard-phone', title: '标准档 · 审批 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: review, render: phoneShell('/vaults', <VaultsPage />) },
  { name: 'protected', title: '保护档 · 沙箱窗口新增「钱包账户」一行（镜像，后续）', width: 520, height: 760, status: 'approved', fixtures, render: () => <SandboxSignMirror /> },
  { name: 'protected-create-window', title: '保护档 · 安全窗口里显示助记词（镜像，后续）', width: 520, height: 820, status: 'approved', fixtures, render: () => <SandboxWordsMirror mode="create" /> },
  { name: 'protected-import-window', title: '保护档 · 安全窗口里输入并核对（镜像，后续）', width: 520, height: 860, status: 'approved', fixtures, render: () => <SandboxWordsMirror mode="import" /> },
  { name: 'protected-phone', title: '保护档 · 沙箱窗口 · 手机（镜像，后续）', width: 390, height: 844, status: 'approved', fixtures, render: () => <SandboxSignMirror /> },
]);

export default designFrames;
