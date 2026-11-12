// Creating a seed-phrase wallet (proposal): set up, write the 12 words down, check them, done.
// The words are a published BIP39 test vector (kit/vault-mnemonic/data.ts), never a wallet.
// Owner decisions 11-12: the wallet is the third option of the signing key's chain selector, and the
// backup is checked by picking three words.
import {
  CreateBackup, CreateCheckQuiz, CreateDone, CreateSetup, ProtectedHandoff,
} from '../../../kit/vault-mnemonic/create';
import { frames } from '../../../frame';
import { alone, avaultFixtures, overPhone } from './fixtures';

const fixtures = avaultFixtures('0.1.11');
const T = '添加密钥';

const designFrames = frames([
  { name: 'setup', title: '新建 · ① 设置 · 签名密钥的链里第三项「助记词 · 多链」', width: 800, height: 1140, status: 'approved', fixtures, render: alone(T, <CreateSetup />) },
  { name: 'setup-phone', title: '新建 · ① 设置 · 手机（三个链选项的宽度）', width: 390, height: 844, status: 'approved', route: '/vaults', fixtures, render: overPhone(T, <CreateSetup />) },
  { name: 'backup-hidden', title: '新建 · ② 抄下助记词 · 点击后才显示', width: 800, height: 900, status: 'approved', fixtures, render: alone(T, <CreateBackup hidden />) },
  { name: 'backup', title: '新建 · ② 抄下助记词 · 已显示', width: 800, height: 900, status: 'approved', fixtures, render: alone(T, <CreateBackup />) },
  { name: 'check-quiz', title: '新建 · ③ 核对 · 点 3 个词', width: 800, height: 900, status: 'approved', fixtures, render: alone(T, <CreateCheckQuiz />) },
  { name: 'check-quiz-correct', title: '新建 · ③ 核对 · 全部选对，可以创建', width: 800, height: 900, status: 'approved', fixtures, render: alone(T, <CreateCheckQuiz state="correct" />) },
  { name: 'check-quiz-wrong', title: '新建 · ③ 核对 · 选错一个', width: 800, height: 960, status: 'approved', fixtures, render: alone(T, <CreateCheckQuiz state="wrong" />) },
  { name: 'done', title: '新建 · 完成 · 账户 #0 的地址和 Agent 的用法', width: 800, height: 900, status: 'approved', fixtures, render: alone(T, <CreateDone />) },
  { name: 'protected-create', title: '保护档 · 新建 · 助记词在安全窗口里生成', width: 800, height: 820, status: 'approved', fixtures, render: alone(T, <ProtectedHandoff source="create" />) },
  { name: 'protected-import', title: '保护档 · 导入 · 助记词在安全窗口里输入', width: 800, height: 820, status: 'approved', fixtures, render: alone(T, <ProtectedHandoff source="import" />) },
  { name: 'backup-phone', title: '新建 · ② 抄下助记词 · 手机', width: 390, height: 844, status: 'approved', route: '/vaults', fixtures, render: overPhone(T, <CreateBackup columns={2} />) },
  { name: 'check-quiz-phone', title: '新建 · ③ 核对 · 手机', width: 390, height: 844, status: 'approved', route: '/vaults', fixtures, render: overPhone(T, <CreateCheckQuiz state="correct" />) },
  { name: 'done-phone', title: '新建 · 完成 · 手机', width: 390, height: 844, status: 'approved', route: '/vaults', fixtures, render: overPhone(T, <CreateDone />) },
]);

export default designFrames;
