// The wallet item: its row in the Vault list, and the accounts dialog with one card per account
// (owner decisions 2026-11-12). Drawn with the shipped page and dialog; the accounts are the server's.
import { VaultHdWalletDialog } from '@/components/ui/vault-hd-wallet-detail';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { frames } from '../../../frame';
import { consolePage } from '../../workbench/fixtures';
import { walletAccounts, walletFixtures, walletSecret } from './fixtures';

const fixtures = walletFixtures;
const noop = () => {};
const dialog = (accounts = walletAccounts) => () => (
  <VaultHdWalletDialog secret={walletSecret(accounts)} open onOpenChange={noop} onChanged={noop} />
);
const page = consolePage(<VaultsPage />);
const rename = ['role=button[name="{t:vaults.hd.wallet.rename}"] >> nth=1'];

const designFrames = frames([
  { name: 'row', title: '列表 · 钱包一行：账户一眼可见', width: 1192, height: 900, status: 'shipped', route: '/vaults', fixtures, render: page },
  { name: 'accounts', title: '钱包 · 每个账户一张卡', width: 860, height: 1180, status: 'shipped', fixtures, render: dialog() },
  { name: 'first', title: '钱包 · 刚创建只有 #0（空状态）', width: 860, height: 900, status: 'shipped', fixtures, render: dialog(walletAccounts.slice(0, 1)) },
  { name: 'rename', title: '钱包 · 重命名账户 #1', width: 860, height: 1180, status: 'shipped', fixtures, click: rename, render: dialog() },
  { name: 'row-phone', title: '列表 · 钱包一行 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, render: page },
  { name: 'accounts-phone', title: '钱包 · 按账户 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, render: consolePage(<><VaultsPage />{dialog()()}</>) },
]);

export default designFrames;
