// Creating a seed-phrase wallet with an optional passphrase (proposal, 3.2.7): the folded section in
// set up, both entries, a reminder on the write-down step, and the recovery line on done. The
// write-down and done steps are the shipped components; the additions are kit.
import { HdBackupStep, HdDoneStep, HdStepFooter, HdStepper } from '@/components/ui/vault-hd-wallet-steps';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { CREATED_ADDRESSES, CREATED_WORDS } from '../../../kit/vault-mnemonic/data';
import { PassphraseBackupReminder, PassphraseRecoveryLine, SetupWithPassphrase, type PassphraseState } from '../../../kit/vault-mnemonic/passphrase';
import { frames } from '../../../frame';
import { consolePage } from '../../workbench/fixtures';
import { alone, avaultFixtures } from './fixtures';

const fixtures = avaultFixtures('0.1.12');
const T = '添加密钥';
const noop = () => {};
const setup = (state: PassphraseState) => () => <SetupWithPassphrase state={state} />;
const backup = () => (
  <form className="flex min-w-0 flex-col gap-4">
    <HdStepper step={2} />
    <HdBackupStep words={CREATED_WORDS} />
    <PassphraseBackupReminder />
    <HdStepFooter step="backup" onBack={noop} onNext={noop} />
  </form>
);
const done = () => (
  <div className="flex min-w-0 flex-col gap-4">
    <HdDoneStep name="AGENT_WALLET" account={CREATED_ADDRESSES[0]} />
    <PassphraseRecoveryLine />
  </div>
);
const show = ['button:has(svg.lucide-eye)'];

const designFrames = frames([
  { name: 'folded', title: 'Passphrase · 默认折叠（大多数人不需要）', width: 800, height: 1000, status: 'proposed', fixtures, render: alone(T, setup('folded')()) },
  { name: 'open', title: 'Passphrase · 展开，两次输入', width: 800, height: 1180, status: 'proposed', fixtures, render: alone(T, setup('empty')()) },
  { name: 'match', title: 'Passphrase · 两次一致，可以生成', width: 800, height: 1180, status: 'proposed', fixtures, render: alone(T, setup('match')()) },
  { name: 'mismatch', title: 'Passphrase · 两次不一致，不能生成', width: 800, height: 1180, status: 'proposed', fixtures, render: alone(T, setup('mismatch')()) },
  { name: 'backup', title: '抄下助记词 · 提醒 passphrase 另外记下', width: 800, height: 1000, status: 'proposed', fixtures, click: show, render: alone(T, backup()) },
  { name: 'done', title: '完成 · 恢复需要词 + 一字不差的 passphrase', width: 800, height: 1100, status: 'proposed', fixtures, render: alone(T, done()) },
  { name: 'match-phone', title: 'Passphrase · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: consolePage(<><VaultsPage />{alone(T, setup('match')())()}</>) },
  { name: 'backup-phone', title: '抄下助记词 · 手机 · passphrase 提醒', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, click: show, render: consolePage(<><VaultsPage />{alone(T, backup())()}</>) },
]);

export default designFrames;
