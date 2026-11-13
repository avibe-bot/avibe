// BTC address-type switch (owner 2026-11-13: B, the label chip): one BTC row whose label — "BTC"
// over the shown type — opens the types the key can sign for. Drawn with the shipped dialog, page
// and rows; the seed wallet and the signing key are the vault fixtures (public test vectors).
import { KeyAddressList } from '@/components/ui/btc-address-row';
import { VaultHdWalletDialog } from '@/components/ui/vault-hd-wallet-detail';
import { HdAccountAddressList } from '@/components/ui/vault-hd-wallet-steps';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { syntheticAddresses } from './fixtures';
import { walletAccounts, walletFixtures, walletSecret } from './mnemonic/fixtures';

const noop = () => {};
const fixtures = walletFixtures;
const dialog = () => <VaultHdWalletDialog secret={walletSecret()} open onOpenChange={noop} onChanged={noop} />;
const chip = ['role=button[name^="{t:vaults.addresses.btcType}"] >> nth=0'];
const unprepared = walletAccounts.map(({ btc_segwit: _s, btc_legacy: _l, bitcoin_address_types: _t, ...rest }) => rest);
const single = () => (
  <div className="flex min-h-full flex-col gap-4 bg-background p-8">
    {/* A seed wallet not yet prepared (or a reader who may not prepare it): Taproot only, with the hint. */}
    <div className="rounded-[10px] border border-edge bg-surface p-3 shadow-elevation-0"><HdAccountAddressList account={unprepared[0]} /></div>
    {/* A protected key: the server offers typed Taproot only, so no chip. */}
    <div className="rounded-[10px] border border-edge bg-surface p-3 shadow-elevation-0"><KeyAddressList addresses={syntheticAddresses} types={['p2tr']} /></div>
  </div>
);

const designFrames = frames([
  { name: 'wallet', title: '钱包账户 · BTC 一行，标签可切换类型', width: 860, height: 1180, status: 'shipped', fixtures, render: dialog },
  { name: 'wallet-open', title: '钱包账户 · 打开 BTC 类型', width: 860, height: 1180, status: 'shipped', fixtures, click: chip, render: dialog },
  { name: 'key-row', title: '签名密钥列表 · BTC 一行', width: 1192, height: 900, status: 'shipped', route: '/vaults', fixtures, render: consolePage(<VaultsPage />) },
  { name: 'key-row-open', title: '签名密钥列表 · 打开 BTC 类型', width: 1192, height: 900, status: 'shipped', route: '/vaults', fixtures, click: chip, render: consolePage(<VaultsPage />) },
  { name: 'single', title: '只有一种可签：未准备的钱包（Taproot + 提示）与保护档密钥（无开关）', width: 760, height: 320, status: 'shipped', fixtures, render: single },
  { name: 'wallet-phone-open', title: '钱包账户 · 手机 · 打开 BTC 类型', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: chip, render: consolePage(<><VaultsPage />{dialog()}</>) },
]);

export default designFrames;
