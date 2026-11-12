// Creating a seed-phrase wallet (proposal): set up, write the 12 words down, check them, done.
// The words are a published BIP39 test vector (kit/vault-mnemonic/data.ts), never a wallet.
// Two real choices are drawn side by side: where the type sits (a third Type, or a third Chain), and
// how the backup is checked (three taps, or one acknowledgement).
import {
  CreateBackup, CreateCheckAcknowledge, CreateCheckQuiz, CreateDone, CreateSetup, ProtectedHandoff,
} from '../../../kit/vault-mnemonic/create';
import { frames } from '../../../frame';
import { alone, avaultFixtures, overPhone } from './fixtures';

const fixtures = avaultFixtures('0.1.11');
const T = '添加助记词钱包';

const designFrames = frames([
  { name: 'setup-type', title: '新建 · ① 设置 · 类型里的第三项「助记词钱包」（推荐 A）', width: 800, height: 1080, status: 'proposed', fixtures, render: alone(T, <CreateSetup picker="kind" />) },
  { name: 'setup-chain', title: '新建 · ① 设置 · 链里的第三项「助记词 · 多链」（备选 B）', width: 800, height: 1140, status: 'proposed', fixtures, render: alone(T, <CreateSetup picker="chain" />) },
  { name: 'backup-hidden', title: '新建 · ② 抄下助记词 · 点击后才显示', width: 800, height: 900, status: 'proposed', fixtures, render: alone(T, <CreateBackup hidden />) },
  { name: 'backup', title: '新建 · ② 抄下助记词 · 已显示', width: 800, height: 900, status: 'proposed', fixtures, render: alone(T, <CreateBackup />) },
  { name: 'check-quiz', title: '新建 · ③ 核对 · 点 3 个词（推荐 A）', width: 800, height: 900, status: 'proposed', fixtures, render: alone(T, <CreateCheckQuiz />) },
  { name: 'check-quiz-correct', title: '新建 · ③ 核对 · 全部选对，可以创建', width: 800, height: 900, status: 'proposed', fixtures, render: alone(T, <CreateCheckQuiz state="correct" />) },
  { name: 'check-quiz-wrong', title: '新建 · ③ 核对 · 选错一个', width: 800, height: 960, status: 'proposed', fixtures, render: alone(T, <CreateCheckQuiz state="wrong" />) },
  { name: 'check-acknowledge', title: '新建 · ③ 核对 · 只勾选确认（备选 B，Phantom 的做法）', width: 800, height: 900, status: 'proposed', fixtures, render: alone(T, <CreateCheckAcknowledge />) },
  { name: 'done', title: '新建 · 完成 · 账户 #0 的地址和 Agent 的用法', width: 800, height: 900, status: 'proposed', fixtures, render: alone(T, <CreateDone />) },
  { name: 'protected-create', title: '保护档 · 新建 · 助记词在安全窗口里生成', width: 800, height: 820, status: 'proposed', fixtures, render: alone(T, <ProtectedHandoff source="create" />) },
  { name: 'protected-import', title: '保护档 · 导入 · 助记词在安全窗口里输入', width: 800, height: 820, status: 'proposed', fixtures, render: alone(T, <ProtectedHandoff source="import" />) },
  { name: 'backup-phone', title: '新建 · ② 抄下助记词 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: overPhone(T, <CreateBackup columns={2} />) },
  { name: 'check-quiz-phone', title: '新建 · ③ 核对 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: overPhone(T, <CreateCheckQuiz state="correct" />) },
  { name: 'done-phone', title: '新建 · 完成 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: overPhone(T, <CreateDone />) },
]);

export default designFrames;
