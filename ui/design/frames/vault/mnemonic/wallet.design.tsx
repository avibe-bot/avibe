// The wallet item (proposal): its row in the Vault list, and the detail with every account. An account
// is one number shared by EVM, Bitcoin Taproot and Solana, as Phantom groups them. Two layouts are
// drawn side by side: one card per account (A, recommended) or one chain at a time (B).
import { WalletDetailAccounts, WalletDetailChains, WalletDetailFirst, WalletRow } from '../../../kit/vault-mnemonic/wallet';
import { frames } from '../../../frame';
import { alone, onCanvas, overPage, overPhone, walletFixtures } from './fixtures';

const fixtures = walletFixtures;
const T = 'AGENT_WALLET';

const designFrames = frames([
  { name: 'row', title: '列表 · 钱包一行：账户一眼可见', width: 1192, height: 220, status: 'proposed', fixtures, render: onCanvas(<ul><WalletRow /></ul>, 1100) },
  { name: 'accounts', title: '钱包 · 按账户（推荐 A）', width: 860, height: 1180, status: 'proposed', fixtures, render: alone(T, <WalletDetailAccounts />, true) },
  { name: 'chains', title: '钱包 · 按链（备选 B）', width: 860, height: 900, status: 'proposed', fixtures, render: alone(T, <WalletDetailChains />, true) },
  { name: 'first', title: '钱包 · 刚创建只有 #0（空状态）', width: 860, height: 900, status: 'proposed', fixtures, render: alone(T, <WalletDetailFirst />, true) },
  { name: 'add', title: '钱包 · 添加账户 #3 并命名', width: 1192, height: 1400, status: 'proposed', route: '/vaults', fixtures, render: overPage(T, <WalletDetailAccounts editing={{ index: 3, kind: 'new' }} />, true) },
  { name: 'rename', title: '钱包 · 重命名账户 #1', width: 860, height: 1180, status: 'proposed', fixtures, render: alone(T, <WalletDetailAccounts editing={{ index: 1, kind: 'rename' }} />, true) },
  { name: 'row-phone', title: '列表 · 钱包一行 · 手机', width: 390, height: 300, status: 'proposed', fixtures, render: onCanvas(<ul><WalletRow /></ul>, 350) },
  { name: 'accounts-phone', title: '钱包 · 按账户 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: overPhone(T, <WalletDetailAccounts />) },
]);

export default designFrames;
