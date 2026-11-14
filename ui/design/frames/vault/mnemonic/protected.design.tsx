// Protected seed-phrase wallets (3.2.7; contract docs/plans/vault-protected-hd-wallets.md rev0.7).
// The page draws the complete shipped Add key dialog and launches the sandbox window; the words,
// passphrase, three-word check and #0 review are the sandbox's own screens, not drawn here. Each state
// is the shipped dialog with its state given through lib/protectedHdDesign, because reaching it for
// real needs the vault sandbox, which no frame may load. Before anything is sent, a failure says
// nothing was saved; after saving started, the wallet stays unconfirmed until an exact match.
import type { ReactElement } from 'react';

import { VaultHdWalletDialog } from '@/components/ui/vault-hd-wallet-detail';
import { ProtectedHdDone } from '@/components/ui/vault-protected-hd-create';
import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import { VaultsPage } from '@/components/workbench/VaultsPage';
import { ProtectedHdDesignState } from '@/lib/protectedHdDesign';
import type { ProtectedHdPhase } from '@/lib/useProtectedHdCreate';

import { IMPORTED_ADDRESSES } from '../../../kit/vault-mnemonic/data';
import { frames } from '../../../frame';
import { consolePage } from '../../workbench/fixtures';
import { alone, avaultFixtures, walletAccounts, walletFixtures, walletSecret } from './fixtures';

const fixtures = avaultFixtures('0.1.12');
const T = '添加密钥';
const noop = () => {};
const create = [
  'role=radio[name="{t:vaults.dialog.kindKeypair}"]',
  'role=radio[name="{t:vaults.hd.chainMnemonic}"]',
  'role=button[name^="{t:vaults.dialog.protectedProtection}"]',
];
const importing = [...create, 'role=radio[name="{t:vaults.dialog.signingImport}"]'];
const add = (phase: ProtectedHdPhase) => () => (
  <ProtectedHdDesignState.Provider value={{ phase, name: 'COLD_WALLET' }}>
    <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />
  </ProtectedHdDesignState.Provider>
);
const phone = (phase: ProtectedHdPhase) => consolePage(<><VaultsPage />{add(phase)()}</>);
const account0 = { ...IMPORTED_ADDRESSES[0] };
const done = alone(T, <ProtectedHdDone name="COLD_WALLET" account0={account0} onFinish={noop} />);
// A protected wallet's accounts: Taproot only for Bitcoin, and every signature approved.
const protectedWallet = { ...walletSecret(walletAccounts.slice(0, 2).map((a) => ({ ...a, always_ask: true, bitcoin_address_types: ['p2tr' as const] }))), name: 'COLD_WALLET', protection: 'protected' as const };
const accounts = (): ReactElement => <VaultHdWalletDialog secret={protectedWallet} open onOpenChange={noop} onChanged={noop} />;

const designFrames = frames([
  { name: 'create', title: '保护档 · 新建 · 完整弹窗，在安全窗口中创建', width: 800, height: 1500, status: 'shipped', fixtures, click: create, render: add({ kind: 'idle' }) },
  { name: 'import', title: '保护档 · 导入 · 完整弹窗，在安全窗口中导入', width: 800, height: 1500, status: 'shipped', fixtures, click: importing, render: add({ kind: 'idle' }) },
  { name: 'waiting', title: '保护档 · 安全窗口已打开', width: 800, height: 1560, status: 'shipped', fixtures, click: create, render: add({ kind: 'waiting' }) },
  { name: 'blocked', title: '保护档 · 弹窗被拦住（提交前，未保存）', width: 800, height: 1560, status: 'shipped', fixtures, click: create, render: add({ kind: 'failed', reason: 'blocked' }) },
  { name: 'cancelled', title: '保护档 · 取消（提交前，未保存）', width: 800, height: 1560, status: 'shipped', fixtures, click: create, render: add({ kind: 'failed', reason: 'cancelled' }) },
  { name: 'closed', title: '保护档 · 窗口被关（提交前，未保存）', width: 800, height: 1560, status: 'shipped', fixtures, click: create, render: add({ kind: 'failed', reason: 'closed' }) },
  { name: 'timeout', title: '保护档 · 5 分钟没完成（提交前，未保存）', width: 800, height: 1560, status: 'shipped', fixtures, click: create, render: add({ kind: 'failed', reason: 'timeout' }) },
  { name: 'reset', title: '保护档 · 期间被锁定或重置（提交前，未保存）', width: 800, height: 1560, status: 'shipped', fixtures, click: create, render: add({ kind: 'failed', reason: 'reset' }) },
  { name: 'saving', title: '保护档 · 窗口完成，正在保存', width: 800, height: 1560, status: 'shipped', fixtures, click: create, render: add({ kind: 'saving' }) },
  { name: 'checking', title: '保护档 · 没有确定回复，向 Avibe 核对（不重来、不再生成）', width: 800, height: 1600, status: 'shipped', fixtures, click: create, render: add({ kind: 'checking', refusal: null }) },
  { name: 'unconfirmed', title: '保护档 · 仍无法确认（显示 Avibe 的拒绝原因，不说“未保存”）', width: 800, height: 1640, status: 'shipped', fixtures, click: create, render: add({ kind: 'unconfirmed', refusal: 'A secret with this name already exists' }) },
  { name: 'mismatch', title: '保护档 · 保存的和窗口生成的对不上，不要转账', width: 800, height: 1600, status: 'shipped', fixtures, click: create, render: add({ kind: 'mismatch' }) },
  { name: 'done', title: '回到页面 · 已创建（保护档）· 恢复说明', width: 800, height: 900, status: 'shipped', fixtures, render: done },
  { name: 'accounts', title: '保护档钱包 · 账户卡：BTC 只有 Taproot，每次签名都批准', width: 860, height: 900, status: 'shipped', fixtures: walletFixtures, render: accounts },
  { name: 'create-phone', title: '保护档 · 新建 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: create, render: phone({ kind: 'idle' }) },
  { name: 'unconfirmed-phone', title: '保护档 · 仍无法确认 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: create, render: phone({ kind: 'unconfirmed', refusal: null }) },
]);

export default designFrames;
