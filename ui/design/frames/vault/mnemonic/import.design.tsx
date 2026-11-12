// Importing an existing phrase: paste, each word checked as it lands, optional passphrase, and
// account #0's addresses to compare with the old wallet before saving. Drawn with the shipped import
// panel; the phrase is the BIP86 test vector, whose #0 matches MetaMask's and Phantom's first account.
import { HdAvaultOld, HdImportFields } from '@/components/ui/vault-hd-wallet-steps';
import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import { checkPhrase, deriveAccounts, wordsToSeed } from '@/lib/vaultHdWallet';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { IMPORTED_WORDS } from '../../../kit/vault-mnemonic/data';
import { frames } from '../../../frame';
import { consolePage } from '../../workbench/fixtures';
import { alone, avaultFixtures } from './fixtures';

const fixtures = avaultFixtures('0.1.11');
const old = avaultFixtures('0.1.10');
const T = '添加密钥';
const noop = () => {};
const importTab = ['role=radio[name="{t:vaults.dialog.kindKeypair}"]', 'role=radio[name="{t:vaults.hd.chainMnemonic}"]', 'role=radio[name="{t:vaults.dialog.signingImport}"]'];
const add = () => <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />;

const panel = (text: string, { passphrase = '', avaultOld = false } = {}) => () => {
  const check = checkPhrase(text);
  const preview = check.ok ? deriveAccounts(wordsToSeed(check.words, passphrase), 1)[0] : null;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <HdImportFields text={text} onTextChange={noop} check={check} passphrase={passphrase} onPassphraseChange={noop} preview={preview} />
      {avaultOld ? <HdAvaultOld version="0.1.11" installed="0.1.10" onOpenDependencies={noop} /> : null}
    </div>
  );
};
const valid = IMPORTED_WORDS.join(' ');
const unknown = IMPORTED_WORDS.map((w, i) => (i === 6 ? 'abandn' : w)).join(' ');
const checksum = IMPORTED_WORDS.map((w, i) => (i === 11 ? 'abandon' : w)).join(' ');

const designFrames = frames([
  { name: 'empty', title: '导入 · 还没粘贴（完整对话框）', width: 800, height: 1320, status: 'shipped', fixtures, click: importTab, render: add },
  { name: 'valid', title: '导入 · 校验通过，核对账户 #0 的地址', width: 800, height: 900, status: 'shipped', fixtures, render: alone(T, panel(valid)()) },
  { name: 'passphrase', title: '导入 · BIP39 密码（地址随之变化）', width: 800, height: 980, status: 'shipped', fixtures, render: alone(T, panel(valid, { passphrase: 'TREZOR' })()) },
  { name: 'unknown-word', title: '错误 · 有一个词不在词表里（给出建议）', width: 800, height: 760, status: 'shipped', fixtures, render: alone(T, panel(unknown)()) },
  { name: 'checksum', title: '错误 · 词都对，校验和不通过', width: 800, height: 760, status: 'shipped', fixtures, render: alone(T, panel(checksum)()) },
  { name: 'avault-old', title: '错误 · avault 版本太旧（不能保存，内容保留）', width: 800, height: 1000, status: 'shipped', fixtures: old, render: alone(T, panel(valid, { avaultOld: true })()) },
  { name: 'valid-phone', title: '导入 · 校验通过 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, render: consolePage(<><VaultsPage />{alone(T, panel(valid)())()}</>) },
  { name: 'unknown-word-phone', title: '错误 · 词不在词表里 · 手机', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures, render: consolePage(<><VaultsPage />{alone(T, panel(unknown)())()}</>) },
]);

export default designFrames;
