// Standard-tier Solana keys (docs/plans/vault-solana.md §8): creating one in the Add dialog, how it
// reads in the list and in Edit, and approving a signature over exact message bytes. Proposed: the
// consumer API is not on the default branch yet, so these are drawn from `kit/vault-solana.tsx`.
// The key is the RFC 8032 test-1 public key and the recipient its test-2 key: published vectors,
// never a wallet. Protected Solana keys and their approval are designed separately.
import { frames } from '../../frame';
import { SolanaCreateDialog, SolanaEditHeader, SolanaKeyList, SolanaSignApproval } from '../../kit/vault-solana';
import { syntheticAddresses, vaultFixtures } from './fixtures';

const fixtures = vaultFixtures();
const address = 'FVen3X669xLzsi6N2V91DoiyzHzg1uAgqiT8jZ9nS96Z';

// A legacy message with one System transfer of 0.01 SOL: 150 bytes.
const transfer =
  '01000103d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'
  + '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c'
  + '0000000000000000000000000000000000000000000000000000000000000000'
  + 'c3a31d0d0fb5cc1047ecf3c8316bb8fdaadee46efcc68d5848a3f6644f597f44'
  + '01020200010c020000008096980000000000';
// 1,180 patterned bytes, near the most one signer's message can hold: the box scrolls, the card does not grow.
const long = Array.from({ length: 1180 }, (_, index) => ((index * 73 + 41) & 0xff).toString(16).padStart(2, '0')).join('');

const approval = (messageHex: string) => () => (
  <SolanaSignApproval keyName="SOL_DEVNET_KEY" address={address} session="Devnet 空投脚本" messageHex={messageHex} />
);

const designFrames = frames([
  {
    name: 'create-generate',
    title: '添加密钥 · Solana 签名密钥 · 生成',
    width: 1000,
    height: 1180,
    status: 'proposed',
    fixtures,
    render: () => <SolanaCreateDialog source="generate" />,
  },
  {
    name: 'create-import',
    title: '添加密钥 · Solana 签名密钥 · 导入',
    width: 1000,
    height: 1280,
    status: 'proposed',
    fixtures,
    render: () => <SolanaCreateDialog source="import" />,
  },
  {
    name: 'create-import-rejected',
    title: '添加密钥 · Solana 导入 · avault 拒绝了粘贴的内容',
    width: 1000,
    height: 1280,
    status: 'proposed',
    fixtures,
    render: () => <SolanaCreateDialog source="import" importText="[12, 34, 56]" importRejected />,
  },
  {
    name: 'create-protected',
    title: '添加密钥 · 已选保护档再选 Solana（保护档上线前）',
    width: 1000,
    height: 1180,
    status: 'proposed',
    fixtures,
    render: () => <SolanaCreateDialog source="generate" tier="protected" />,
  },
  {
    name: 'create-phone',
    title: '添加密钥 · Solana · 手机',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures,
    render: () => <SolanaCreateDialog source="generate" />,
  },
  {
    name: 'list',
    title: '密钥列表 · Solana 密钥只有一行 Solana 地址',
    width: 900,
    height: 370,
    status: 'proposed',
    fixtures,
    render: () => <SolanaKeyList evmAddresses={syntheticAddresses} solanaAddress={address} />,
  },
  {
    name: 'edit',
    title: '编辑密钥 · 链与类型、档位一样不可更改',
    width: 1000,
    height: 560,
    status: 'proposed',
    fixtures,
    render: () => <SolanaEditHeader name="SOL_DEVNET_KEY" />,
  },
  {
    name: 'approval',
    title: '审批 · 标准档 Solana 密钥签名（每次都问）',
    width: 1000,
    height: 720,
    status: 'proposed',
    fixtures,
    render: approval(transfer),
  },
  {
    name: 'approval-long',
    title: '审批 · 长消息在框内滚动',
    width: 1000,
    height: 760,
    status: 'proposed',
    fixtures,
    render: approval(long),
  },
  {
    name: 'approval-empty',
    title: '审批 · 空消息也是合法消息',
    width: 1000,
    height: 620,
    status: 'proposed',
    fixtures,
    render: approval(''),
  },
  {
    name: 'approval-phone',
    title: '审批 · Solana 签名 · 手机',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures,
    render: approval(transfer),
  },
]);

export default designFrames;
