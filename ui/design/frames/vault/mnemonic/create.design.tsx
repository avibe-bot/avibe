// Creating a seed-phrase wallet in the Add key dialog: set up, write the 12 words down, check them,
// done. Drawn with the shipped components; the words are a published BIP39 test vector
// (kit/vault-mnemonic/data.ts), never a wallet. Owner decisions 2026-11-12: the wallet is the third
// option of the signing key's chain selector, and the backup is checked by picking three words.
import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import {
  HdBackupStep, HdCheckStep, HdDoneStep, HdStepFooter, HdStepper,
} from '@/components/ui/vault-hd-wallet-steps';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { CREATED_ADDRESSES, CREATED_WORDS } from '../../../kit/vault-mnemonic/data';
import { ProtectedHandoff } from '../../../kit/vault-mnemonic/create';
import { frames } from '../../../frame';
import { consolePage } from '../../workbench/fixtures';
import { alone, avaultFixtures, checkFixtures, type CheckFixture } from './fixtures';

const fixtures = avaultFixtures('0.1.11');
const T = '添加密钥';
const noop = () => {};
const setup = ['role=radio[name="{t:vaults.dialog.kindKeypair}"]', 'role=radio[name="{t:vaults.hd.chainMnemonic}"]'];
const add = () => <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />;
const show = ['button:has(svg.lucide-eye)'];

const backup = () => (
  <form className="flex min-w-0 flex-col gap-4">
    <HdStepper step={2} />
    <HdBackupStep words={CREATED_WORDS} />
    <HdStepFooter step="backup" onBack={noop} onNext={noop} />
  </form>
);
const check = (state: CheckFixture) => () => (
  <form className="flex min-w-0 flex-col gap-4">
    <HdStepper step={3} />
    <HdCheckStep quiz={state.quiz} chosen={state.chosen} onChoose={noop} />
    <HdStepFooter step="check" onBack={noop} canCreate={state.canCreate} />
  </form>
);
const done = () => (
  <div className="flex min-w-0 flex-col gap-4">
    <HdDoneStep name="AGENT_WALLET" account={CREATED_ADDRESSES[0]} />
  </div>
);

const designFrames = frames([
  { name: 'setup', title: '新建 · ① 设置 · 签名密钥的链里第三项「助记词钱包」', width: 800, height: 1320, status: 'shipped', fixtures, click: setup, render: add },
  { name: 'setup-phone', title: '新建 · ① 设置 · 手机（三个链选项的宽度）', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: setup, render: consolePage(<><VaultsPage />{add()}</>) },
  { name: 'backup-hidden', title: '新建 · ② 抄下助记词 · 点击后才显示', width: 800, height: 900, status: 'shipped', fixtures, render: alone(T, backup()) },
  { name: 'backup', title: '新建 · ② 抄下助记词 · 已显示', width: 800, height: 900, status: 'shipped', fixtures, click: show, render: alone(T, backup()) },
  { name: 'check-quiz', title: '新建 · ③ 核对 · 点 3 个词', width: 800, height: 900, status: 'shipped', fixtures, render: alone(T, check(checkFixtures.empty)()) },
  { name: 'check-quiz-correct', title: '新建 · ③ 核对 · 全部选对，可以创建', width: 800, height: 900, status: 'shipped', fixtures, render: alone(T, check(checkFixtures.correct)()) },
  { name: 'check-quiz-wrong', title: '新建 · ③ 核对 · 选错一个', width: 800, height: 960, status: 'shipped', fixtures, render: alone(T, check(checkFixtures.wrong)()) },
  { name: 'done', title: '新建 · 完成 · 账户 #0 的地址（已和这些词核对）', width: 800, height: 960, status: 'shipped', fixtures, render: alone(T, done()) },
  { name: 'backup-phone', title: '新建 · ② 抄下助记词 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, click: show, render: consolePage(<><VaultsPage />{alone(T, backup())()}</>) },
  { name: 'check-quiz-phone', title: '新建 · ③ 核对 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, render: consolePage(<><VaultsPage />{alone(T, check(checkFixtures.correct)())()}</>) },
  { name: 'done-phone', title: '新建 · 完成 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, render: consolePage(<><VaultsPage />{alone(T, done())()}</>) },
  // Protected seed-phrase wallets live in the sandbox's own window (vault-sandbox), after 3.2.6.
  { name: 'protected-create', title: '保护档 · 新建 · 助记词在安全窗口里生成（后续）', width: 800, height: 1060, status: 'approved', fixtures, render: alone(T, <ProtectedHandoff source="create" />) },
  { name: 'protected-import', title: '保护档 · 导入 · 助记词在安全窗口里输入（后续）', width: 800, height: 1060, status: 'approved', fixtures, render: alone(T, <ProtectedHandoff source="import" />) },
]);

export default designFrames;
