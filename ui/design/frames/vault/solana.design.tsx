// What a standard-tier Solana key adds to the Vault (docs/plans/vault-solana.md §8). Each frame draws
// only the new part, at the width its dialog gives it: a desktop dialog is 512px wide with 24px of
// padding, a phone sheet is the 390px screen with 20px. The surface around it is the shipped one
// (`vault/secret-dialog/keypair`, `vault/page/*`, `vault/approval/sign`) and does not change.
// Proposed: the consumer API is not on the default branch yet. The key is the RFC 8032 test-1 public
// key and the recipient its test-2 key: published vectors, never a wallet. Protected Solana keys and
// their approval window are designed separately.
import type { ReactNode } from 'react';

import { SigningAddressRow } from '@/components/ui/signing-address-list';

import { frames } from '../../frame';
import { SolanaKeyBuilder, SolanaProtectedNotice, SolanaSignRows } from '../../kit/vault-solana';
import { vaultFixtures } from './fixtures';

const fixtures = vaultFixtures();
const address = 'FVen3X669xLzsi6N2V91DoiyzHzg1uAgqiT8jZ9nS96Z';
const session = { id: 'ses_airdrop', label: 'Devnet 空投脚本', isWorkbench: true, isIdFallback: false };

// A legacy message with one System transfer of 0.01 SOL: 150 bytes.
const transfer =
  '01000103d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'
  + '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c'
  + '0000000000000000000000000000000000000000000000000000000000000000'
  + 'c3a31d0d0fb5cc1047ecf3c8316bb8fdaadee46efcc68d5848a3f6644f597f44'
  + '01020200010c020000008096980000000000';
// 1,180 patterned bytes, near the most one signer's message can hold: the box scrolls, the card does not grow.
const long = Array.from({ length: 1180 }, (_, index) => ((index * 73 + 41) & 0xff).toString(16).padStart(2, '0')).join('');

const desktop = (node: ReactNode) => () => <div className="p-6">{node}</div>;
const phone = (node: ReactNode) => () => <div className="p-5">{node}</div>;
const part = (name: string, title: string, height: number, node: ReactNode, onPhone = false) => ({
  name,
  title,
  width: onPhone ? 390 : 512,
  height,
  status: 'proposed' as const,
  fixtures,
  render: onPhone ? phone(node) : desktop(node),
});

const designFrames = frames([
  part('create-generate', '添加密钥 · 签名密钥选 Solana · 生成', 260, <SolanaKeyBuilder source="generate" />),
  part('create-import', '添加密钥 · 签名密钥选 Solana · 导入', 330, <SolanaKeyBuilder source="import" />),
  part('create-import-failed', '添加密钥 · Solana 导入 · avault 没有导入粘贴的内容', 330, <SolanaKeyBuilder source="import" importText="[12, 34, 56]" importFailed />),
  part('create-protected', '添加密钥 · 选了保护档的 Solana 密钥（保护档上线前）', 120, <SolanaProtectedNotice />),
  part('create-generate-phone', '添加密钥 · Solana · 生成 · 手机', 280, <SolanaKeyBuilder source="generate" />, true),
  part('create-import-phone', '添加密钥 · Solana · 导入 · 手机', 360, <SolanaKeyBuilder source="import" />, true),
  part('address', '密钥列表 · Solana 密钥只有这一行地址', 80, <SigningAddressRow label="Solana" value={address} />),
  part('address-phone', '密钥列表 · Solana 地址 · 手机', 80, <SigningAddressRow label="Solana" value={address} />, true),
  part('approval', '审批 · 标准档 Solana 密钥签名 · 摘要（详情收起）', 230, <SolanaSignRows address={address} session={session} messageHex={transfer} />),
  part('approval-details', '审批 · 详情展开：完整地址与完整待签名字节', 440, <SolanaSignRows address={address} session={session} messageHex={transfer} details />),
  part('approval-details-long', '审批 · 长消息在详情框内滚动', 480, <SolanaSignRows address={address} session={session} messageHex={long} details />),
  part('approval-empty', '审批 · 空消息也是合法消息（详情展开）', 240, <SolanaSignRows address={address} session={session} messageHex="" details />),
  part('approval-phone', '审批 · 摘要（详情收起）· 手机', 230, <SolanaSignRows address={address} session={session} messageHex={transfer} />, true),
  part('approval-details-phone', '审批 · 详情展开 · 手机', 520, <SolanaSignRows address={address} session={session} messageHex={transfer} details />, true),
]);

export default designFrames;
