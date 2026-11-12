// How a signature request names the wallet account (proposal). Standard tier: the approval card adds
// an Account row and the path in Details. Protected tier: the sandbox window adds the account number
// and path from authenticated metadata (mirrored here in product tokens; the real one is
// vault-sandbox design/signing.html). The message is the 150-byte 0.01 SOL transfer of vault/solana.
import { SandboxSignMirror, SandboxWordsMirror, WalletSignCard } from '../../../kit/vault-mnemonic/signing';
import { frames } from '../../../frame';
import { alone, overPage, overPhone, walletFixtures } from './fixtures';

const fixtures = walletFixtures;
const T = 'Agent 想要签名';

const designFrames = frames([
  { name: 'standard', title: '标准档 · 审批卡写明账户 #1 和 Solana 地址', width: 1192, height: 900, status: 'approved', route: '/vaults', fixtures, render: overPage(T, <WalletSignCard />) },
  { name: 'standard-details', title: '标准档 · 详情里有派生路径', width: 800, height: 760, status: 'approved', fixtures, render: alone(T, <WalletSignCard open />) },
  { name: 'protected', title: '保护档 · 沙箱窗口新增「钱包账户」一行（镜像）', width: 520, height: 760, status: 'approved', fixtures, render: () => <SandboxSignMirror /> },
  { name: 'protected-create-window', title: '保护档 · 安全窗口里显示助记词（镜像）', width: 520, height: 820, status: 'approved', fixtures, render: () => <SandboxWordsMirror mode="create" /> },
  { name: 'protected-import-window', title: '保护档 · 安全窗口里输入并核对（镜像）', width: 520, height: 860, status: 'approved', fixtures, render: () => <SandboxWordsMirror mode="import" /> },
  { name: 'standard-phone', title: '标准档 · 审批 · 手机', width: 390, height: 844, status: 'approved', route: '/vaults', fixtures, render: overPhone(T, <WalletSignCard />) },
  { name: 'protected-phone', title: '保护档 · 沙箱窗口 · 手机（镜像）', width: 390, height: 844, status: 'approved', fixtures, render: () => <SandboxSignMirror /> },
]);

export default designFrames;
