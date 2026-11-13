// Protected-tier seed-phrase wallets (proposal for the owner, 3.2.7; contract rev0.6). The page draws
// the complete Add key dialog and launches the secure window; words, passphrase, the three-word check
// and the #0 review happen only in that window. The standard tier's create-passphrase frames are
// vault/mnemonic/create-passphrase/*; the shipped standard steps are vault/mnemonic/create|import|wallet.
import type { ReactElement } from 'react';

import { frames } from '../../../frame';
import {
  ProtectedAccountCards, ProtectedDone, ProtectedHdDialog, WindowCheck, WindowImport, WindowPassphrase, WindowReview, WindowSignBitcoin, WindowWords,
} from '../../../kit/vault-mnemonic/protected-hd';
import { alone, avaultFixtures, overPhone } from './fixtures';

const fixtures = avaultFixtures('0.1.12');
const T = '添加密钥';
const dialog = (node: ReactElement) => alone(T, node);
const win = (node: ReactElement) => () => node;

const designFrames = frames([
  { name: 'create-launch', title: '保护档 · 新建 · 完整弹窗，在安全窗口里继续', width: 800, height: 1500, status: 'proposed', fixtures, render: dialog(<ProtectedHdDialog source="create" />) },
  { name: 'import-launch', title: '保护档 · 导入 · 完整弹窗，在安全窗口里继续', width: 800, height: 1500, status: 'proposed', fixtures, render: dialog(<ProtectedHdDialog source="import" />) },
  { name: 'waiting', title: '保护档 · 安全窗口已打开，等它完成', width: 800, height: 1560, status: 'proposed', fixtures, render: dialog(<ProtectedHdDialog source="create" state="waiting" />) },
  { name: 'blocked', title: '保护档 · 弹窗被浏览器拦住', width: 800, height: 1560, status: 'proposed', fixtures, render: dialog(<ProtectedHdDialog source="create" state="blocked" />) },
  { name: 'cancelled', title: '保护档 · 取消了，什么都没保存', width: 800, height: 1560, status: 'proposed', fixtures, render: dialog(<ProtectedHdDialog source="create" state="cancelled" />) },
  { name: 'timeout', title: '保护档 · 5 分钟没完成', width: 800, height: 1560, status: 'proposed', fixtures, render: dialog(<ProtectedHdDialog source="create" state="timeout" />) },
  { name: 'window-words', title: '安全窗口 · 新建 1/4 · 抄下助记词', width: 520, height: 820, status: 'proposed', fixtures, render: win(<WindowWords />) },
  { name: 'window-check', title: '安全窗口 · 新建 2/4 · 点 3 个词', width: 520, height: 820, status: 'proposed', fixtures, render: win(<WindowCheck />) },
  { name: 'window-check-wrong', title: '安全窗口 · 新建 2/4 · 选错一个', width: 520, height: 860, status: 'proposed', fixtures, render: win(<WindowCheck wrong />) },
  { name: 'window-passphrase', title: '安全窗口 · 新建 3/4 · Passphrase（可选，两次）', width: 520, height: 760, status: 'proposed', fixtures, render: win(<WindowPassphrase mode="create" state="match" />) },
  { name: 'window-passphrase-mismatch', title: '安全窗口 · 新建 3/4 · 两次不一致', width: 520, height: 760, status: 'proposed', fixtures, render: win(<WindowPassphrase mode="create" state="mismatch" />) },
  { name: 'window-review', title: '安全窗口 · 新建 4/4 · 账户 #0，用通行密钥保存', width: 520, height: 760, status: 'proposed', fixtures, render: win(<WindowReview mode="create" />) },
  { name: 'window-import', title: '安全窗口 · 导入 1/3 · 粘贴并逐词检查', width: 520, height: 760, status: 'proposed', fixtures, render: win(<WindowImport />) },
  { name: 'window-import-passphrase', title: '安全窗口 · 导入 2/3 · 原钱包的 Passphrase', width: 520, height: 760, status: 'proposed', fixtures, render: win(<WindowPassphrase mode="import" state="empty" />) },
  { name: 'window-import-review', title: '安全窗口 · 导入 3/3 · 和原钱包核对，保存', width: 520, height: 760, status: 'proposed', fixtures, render: win(<WindowReview mode="import" />) },
  { name: 'done', title: '回到页面 · 已创建（保护档）· 恢复说明', width: 800, height: 900, status: 'proposed', fixtures, render: dialog(<ProtectedDone />) },
  { name: 'accounts', title: '保护档钱包 · 账户卡：BTC 只有 Taproot，没有「签名前问我」开关', width: 760, height: 520, status: 'proposed', fixtures, render: win(<ProtectedAccountCards />) },
  { name: 'sign-bitcoin', title: '安全窗口 · 签名 · 账户 #1 · Taproot 和路径', width: 520, height: 760, status: 'proposed', fixtures, render: win(<WindowSignBitcoin />) },
  { name: 'create-launch-phone', title: '保护档 · 新建 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: overPhone(T, <ProtectedHdDialog source="create" />) },
  { name: 'window-words-phone', title: '安全窗口 · 抄下助记词 · 手机', width: 390, height: 844, status: 'proposed', fixtures, render: win(<WindowWords />) },
]);

export default designFrames;
