// Creating a seed-phrase wallet with an optional passphrase (owner approved 2026-11-13, 3.2.7). Set up is the
// whole shipped Add key dialog with every field (kind, name, chain, key source, description, tags, tier,
// ask for #0): the folded 「添加 Passphrase（可选）」 sits right under the key-source hint, the step it
// changes, before the fields every key has. The typed states draw the shipped section on its own (a
// frame cannot type); write-down and done are the shipped steps.
import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import { HdBackupStep, HdCreatePassphrase, HdDoneStep, HdStepFooter, HdStepper } from '@/components/ui/vault-hd-wallet-steps';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { CREATED_ADDRESSES, CREATED_WORDS } from '../../../kit/vault-mnemonic/data';
import { frames } from '../../../frame';
import { consolePage } from '../../workbench/fixtures';
import { alone, avaultFixtures } from './fixtures';

const fixtures = avaultFixtures('0.1.12');
const T = '添加密钥';
const noop = () => {};
const setup = ['role=radio[name="{t:vaults.dialog.kindKeypair}"]', 'role=radio[name="{t:vaults.hd.chainMnemonic}"]'];
const expand = [...setup, 'role=button[name^="{t:vaults.hd.passphrase.title}"]'];
const add = () => <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />;
const show = ['button:has(svg.lucide-eye)'];
const typed = (value: string, again: string) => () => (
  <div className="flex min-w-0 flex-col gap-4">
    <HdCreatePassphrase value={value} confirm={again} onChange={noop} onConfirmChange={noop} />
  </div>
);
const backup = () => (
  <form className="flex min-w-0 flex-col gap-4">
    <HdStepper step={2} />
    <HdBackupStep words={CREATED_WORDS} withPassphrase />
    <HdStepFooter step="backup" onBack={noop} onNext={noop} />
  </form>
);
const done = () => (
  <div className="flex min-w-0 flex-col gap-4">
    <HdDoneStep name="AGENT_WALLET" account={CREATED_ADDRESSES[0]} withPassphrase />
  </div>
);

const designFrames = frames([
  { name: 'setup-folded', title: '新建 · 完整弹窗 · Passphrase 默认折叠（在密钥来源下面）', width: 800, height: 1420, status: 'shipped', fixtures, click: setup, render: add },
  { name: 'setup-expanded', title: '新建 · 完整弹窗 · Passphrase 展开（两栏空着 = 不设置）', width: 800, height: 1700, status: 'shipped', fixtures, click: expand, render: add },
  { name: 'setup-folded-phone', title: '新建 · 完整弹窗 · 手机 · 折叠', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: setup, render: consolePage(<><VaultsPage />{add()}</>) },
  { name: 'setup-expanded-phone', title: '新建 · 完整弹窗 · 手机 · 展开', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: expand, render: consolePage(<><VaultsPage />{add()}</>) },
  { name: 'typed-match', title: 'Passphrase 区块 · 两次一致（单独画，帧里不能打字）', width: 800, height: 520, status: 'shipped', fixtures, render: alone(T, typed('correct horse battery', 'correct horse battery')()) },
  { name: 'typed-mismatch', title: 'Passphrase 区块 · 两次不一致，「生成助记词」不可点', width: 800, height: 520, status: 'shipped', fixtures, render: alone(T, typed('correct horse battery', 'correct horse batery')()) },
  { name: 'backup', title: '抄下助记词 · 提醒 Passphrase 另外记下', width: 800, height: 1000, status: 'shipped', fixtures, click: show, render: alone(T, backup()) },
  { name: 'done', title: '完成 · 恢复需要词 + 一字不差的 Passphrase（替换原来的灰字）', width: 800, height: 1100, status: 'shipped', fixtures, render: alone(T, done()) },
  { name: 'backup-phone', title: '抄下助记词 · 手机 · Passphrase 提醒', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: show, render: consolePage(<><VaultsPage />{alone(T, backup())()}</>) },
]);

export default designFrames;
