// What a standard-tier Solana key adds to the Vault (docs/plans/vault-solana.md §8). Each frame draws
// only the new part, at the width it has in its dialog or list: the surface around it is the shipped
// one (`vault/secret-dialog/keypair`, `vault/page/*`, `vault/approval/sign`) and does not change.
// Proposed: the consumer API is not on the default branch yet. The key is the RFC 8032 test-1 public
// key and the recipient its test-2 key: published vectors, never a wallet. Protected Solana keys and
// their approval window are designed separately.
import { SigningAddressRow } from '@/components/ui/signing-address-list';

import { frames } from '../../frame';
import { SolanaKeyBuilder, SolanaProtectedNotice, SolanaSignRows } from '../../kit/vault-solana';
import { vaultFixtures } from './fixtures';

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

const designFrames = frames([
  {
    name: 'create-generate',
    title: '添加密钥 · 签名密钥选 Solana · 生成',
    width: 560,
    height: 240,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaKeyBuilder source="generate" /></div>,
  },
  {
    name: 'create-import',
    title: '添加密钥 · 签名密钥选 Solana · 导入',
    width: 560,
    height: 330,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaKeyBuilder source="import" /></div>,
  },
  {
    name: 'create-import-rejected',
    title: '添加密钥 · Solana 导入 · avault 拒绝了粘贴的内容',
    width: 560,
    height: 320,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaKeyBuilder source="import" importText="[12, 34, 56]" importRejected /></div>,
  },
  {
    name: 'create-protected',
    title: '添加密钥 · 选了保护档的 Solana 密钥（保护档上线前）',
    width: 560,
    height: 100,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaProtectedNotice /></div>,
  },
  {
    name: 'create-phone',
    title: '添加密钥 · Solana · 手机宽度',
    width: 390,
    height: 270,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-4"><SolanaKeyBuilder source="generate" /></div>,
  },
  {
    name: 'address',
    title: '密钥列表 · Solana 密钥只有这一行地址',
    width: 720,
    height: 80,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SigningAddressRow label="Solana" value={address} /></div>,
  },
  {
    name: 'address-phone',
    title: '密钥列表 · Solana 地址 · 手机宽度',
    width: 390,
    height: 80,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-4"><SigningAddressRow label="Solana" value={address} /></div>,
  },
  {
    name: 'approval',
    title: '审批 · 标准档 Solana 密钥签名 · 摘要',
    width: 560,
    height: 220,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaSignRows address={address} messageHex={transfer} /></div>,
  },
  {
    name: 'approval-details',
    title: '审批 · 展开详情：完整的待签名字节',
    width: 560,
    height: 360,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaSignRows address={address} messageHex={transfer} details /></div>,
  },
  {
    name: 'approval-details-long',
    title: '审批 · 长消息在详情框内滚动',
    width: 560,
    height: 400,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaSignRows address={address} messageHex={long} details /></div>,
  },
  {
    name: 'approval-empty',
    title: '审批 · 空消息也是合法消息',
    width: 560,
    height: 120,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-6"><SolanaSignRows address={address} messageHex="" /></div>,
  },
  {
    name: 'approval-phone',
    title: '审批 · Solana 签名 · 手机宽度',
    width: 390,
    height: 440,
    status: 'proposed',
    fixtures,
    render: () => <div className="p-4"><SolanaSignRows address={address} messageHex={transfer} details /></div>,
  },
]);

export default designFrames;
