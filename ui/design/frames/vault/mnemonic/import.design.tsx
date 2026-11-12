// Importing an existing phrase (proposal): paste, each word checked as it lands, optional passphrase,
// and account #0's addresses to compare with the old wallet before saving. The phrase is the BIP86
// test vector; its addresses match MetaMask's and Phantom's first account for that phrase.
import { ImportForm } from '../../../kit/vault-mnemonic/import';
import { frames } from '../../../frame';
import { alone, avaultFixtures, overPhone } from './fixtures';

const fixtures = avaultFixtures('0.1.11');
const old = avaultFixtures('0.1.10');
const T = '导入助记词钱包';

const designFrames = frames([
  { name: 'empty', title: '导入 · 还没粘贴', width: 1000, height: 1080, status: 'proposed', fixtures, render: alone(T, <ImportForm state="empty" />) },
  { name: 'valid', title: '导入 · 校验通过，核对账户 #0 的地址', width: 1000, height: 1340, status: 'proposed', fixtures, render: alone(T, <ImportForm state="valid" />) },
  { name: 'passphrase', title: '导入 · 展开 BIP39 密码', width: 1000, height: 1440, status: 'proposed', fixtures, render: alone(T, <ImportForm state="valid" passphraseOpen />) },
  { name: 'unknown-word', title: '错误 · 有一个词不在词表里（给出建议）', width: 1000, height: 1200, status: 'proposed', fixtures, render: alone(T, <ImportForm state="unknown-word" />) },
  { name: 'checksum', title: '错误 · 词都对，校验和不通过', width: 1000, height: 1200, status: 'proposed', fixtures, render: alone(T, <ImportForm state="checksum" />) },
  { name: 'avault-old', title: '错误 · avault 版本太旧（不能保存，内容保留）', width: 1000, height: 1460, status: 'proposed', fixtures: old, render: alone(T, <ImportForm state="avault-old" />) },
  { name: 'valid-phone', title: '导入 · 校验通过 · 手机（滚动到核对地址）', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: overPhone(T, <ImportForm state="valid" scrolled />) },
  { name: 'unknown-word-phone', title: '错误 · 词不在词表里 · 手机（滚动到助记词）', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures, render: overPhone(T, <ImportForm state="unknown-word" scrolled />) },
]);

export default designFrames;
