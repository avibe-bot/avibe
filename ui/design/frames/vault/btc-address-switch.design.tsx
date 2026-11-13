// BTC address-type switch (owner 2026-11-13 14:01): one BTC row with a small switch at its label,
// on a seed wallet's account cards, the #0 check after create and in the import preview, and a
// signing key's address list. Two variants for the owner to choose: A segmented pill, B chip menu.
import type { ReactNode } from 'react';
import { Lock } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { SigningAddressRow } from '@/components/ui/signing-address-list';
import { AccountIndex } from '@/components/ui/vault-hd-account-index';

import { frames } from '../../frame';
import { BtcRowChip, BtcRowSegmented, L } from '../../kit/btc-address-switch/parts';
import { KEY_ADDRESSES, ONLY_TAPROOT, SEED_ACCOUNT0, SEED_ACCOUNT1, type BtcAddresses } from '../../kit/btc-address-switch/data';
import { systemFixtures } from '../system/fixtures';

const fixtures = systemFixtures();
type Variant = 'A' | 'B';
// B's chip ("BTC · Taproot ▾") is wider than a plain label, so B widens the label column for every
// row in the list to keep the addresses and copy buttons aligned.
const col = (v: Variant) => (v === 'B' ? 'w-[104px]' : undefined);
const Btc = ({ v, addresses, open, label }: { v: Variant; addresses: BtcAddresses; open?: boolean; label?: string }) =>
  v === 'A' ? <BtcRowSegmented addresses={addresses} labelClassName={label} /> : <BtcRowChip addresses={addresses} open={open} labelClassName={label} />;

const Canvas = ({ children, phone }: { children: ReactNode; phone?: boolean }) => (
  <div className={phone ? 'min-h-full bg-background p-3' : 'min-h-full bg-background p-8'}>
    <div className={phone ? 'flex flex-col gap-3' : 'mx-auto flex max-w-[620px] flex-col gap-4'}>{children}</div>
  </div>
);
const Card = ({ children, tint }: { children: ReactNode; tint?: boolean }) => (
  <div className={tint ? 'flex min-w-0 flex-col gap-2 rounded-[10px] border border-mint/40 bg-mint-soft/40 p-3' : 'flex min-w-0 flex-col gap-2 rounded-[10px] border border-edge bg-surface p-3 shadow-elevation-0'}>{children}</div>
);
const Title = ({ children }: { children: ReactNode }) => <span className="text-[13.5px] font-semibold text-foreground">{children}</span>;

const account = (v: Variant, a: typeof SEED_ACCOUNT0, index: number, name: ReactNode, open?: boolean) => (
  <Card>
    <div className="flex items-center gap-2"><AccountIndex index={index} /><Title>{name}</Title></div>
    <div className="flex min-w-0 flex-col gap-1">
      <SigningAddressRow label="ETH" value={a.eth} labelClassName={col(v)} />
      <Btc v={v} addresses={a.btc} open={open} label={col(v)} />
      <SigningAddressRow label="Solana" value={a.solana} labelClassName={col(v)} />
    </div>
  </Card>
);

const walletCards = (v: Variant, phone = false, open = false) => () => (
  <Canvas phone={phone}>
    {account(v, SEED_ACCOUNT0, 0, <L zh="主账户" en="Main" />, open)}
    {account(v, SEED_ACCOUNT1, 1, 'trading')}
  </Canvas>
);
const keyRow = (v: Variant) => () => (
  <Canvas>
    <Card>
      <div className="flex items-center gap-2"><Title>ETH_HOT_KEY</Title><Badge variant="secondary"><L zh="标准档" en="Standard" /></Badge></div>
      <div className="flex min-w-0 flex-col gap-1">
        <SigningAddressRow label="ETH" value={KEY_ADDRESSES.eth!} labelClassName={col(v)} />
        <Btc v={v} addresses={{ taproot: KEY_ADDRESSES.btc_taproot, segwit: KEY_ADDRESSES.btc_segwit, legacy: KEY_ADDRESSES.btc_legacy }} label={col(v)} />
      </div>
    </Card>
    <Card>
      <div className="flex items-center gap-2"><Title>COLD_BTC</Title><Badge variant="warning"><Lock className="size-3" /><L zh="保护档" en="Protected" /></Badge></div>
      <div className="flex min-w-0 flex-col gap-1">
        {/* A protected key signs typed Taproot only today: one address, no switch. */}
        <Btc v={v} addresses={ONLY_TAPROOT} label={col(v)} />
      </div>
    </Card>
  </Canvas>
);
const check = (v: Variant, phone = false) => () => (
  <Canvas phone={phone}>
    <Card tint>
      <div className="flex items-center gap-2"><AccountIndex index={0} /><Title><L zh="和原钱包的第一个账户核对" en="Compare with your old wallet’s first account" /></Title></div>
      <div className="flex min-w-0 flex-col gap-1">
        <SigningAddressRow label="ETH" value={SEED_ACCOUNT0.eth} labelClassName={col(v)} />
        <Btc v={v} addresses={SEED_ACCOUNT0.btc} label={col(v)} />
        <SigningAddressRow label="Solana" value={SEED_ACCOUNT0.solana} labelClassName={col(v)} />
      </div>
    </Card>
  </Canvas>
);

const designFrames = frames([
  { name: 'a-wallet-cards', title: 'A · 分段胶囊 · 助记词钱包账户卡', width: 760, height: 420, status: 'proposed', fixtures, render: walletCards('A') },
  { name: 'a-segwit-picked', title: 'A · 分段胶囊 · 切到 SegWit', width: 760, height: 420, status: 'proposed', fixtures, click: ['role=radio[name="SegWit"] >> nth=0'], render: walletCards('A') },
  { name: 'a-key-row', title: 'A · 分段胶囊 · 签名密钥（标准三种；保护档只有 Taproot，无开关）', width: 760, height: 360, status: 'proposed', fixtures, render: keyRow('A') },
  { name: 'a-check', title: 'A · 分段胶囊 · 创建/导入时的 #0 核对', width: 760, height: 260, status: 'proposed', fixtures, render: check('A') },
  { name: 'a-phone', title: 'A · 分段胶囊 · 手机', width: 390, height: 560, status: 'proposed', fixtures, render: walletCards('A', true) },
  { name: 'b-wallet-cards', title: 'B · 标签小菜单 · 助记词钱包账户卡', width: 760, height: 380, status: 'proposed', fixtures, render: walletCards('B') },
  { name: 'b-open', title: 'B · 标签小菜单 · 展开', width: 760, height: 380, status: 'proposed', fixtures, click: ['button[aria-label^="BTC"] >> nth=0'], render: walletCards('B') },
  { name: 'b-key-row', title: 'B · 标签小菜单 · 签名密钥（标准三种；保护档只有 Taproot，无开关）', width: 760, height: 320, status: 'proposed', fixtures, render: keyRow('B') },
  { name: 'b-check', title: 'B · 标签小菜单 · 创建/导入时的 #0 核对', width: 760, height: 240, status: 'proposed', fixtures, render: check('B') },
  { name: 'b-phone', title: 'B · 标签小菜单 · 手机', width: 390, height: 520, status: 'proposed', fixtures, render: walletCards('B', true) },
  { name: 'b-phone-open', title: 'B · 标签小菜单 · 手机展开', width: 390, height: 520, status: 'proposed', fixtures, click: ['button[aria-label^="BTC"] >> nth=0'], render: walletCards('B', true) },
]);

export default designFrames;
