// Generate a password or application secret inside Vaults (avibe#3338; owner brief 2026-11-13):
// proposed. One generator in three places: the value when adding a static secret, a field row when
// editing, and the Agent's request card. Two variants for the single value: A, a third value source
// "Generate" with its options open; B, a dice button in the value field that fills it at once, with the
// options folded under it. Standard generates in this page; Protected only carries the options and
// generates inside the secure window when the secret is saved. Every dialog is drawn complete, field
// for field as shipped (kit/vault-generate/dialogs.tsx); the sample values are not credentials.
import type { ReactNode } from 'react';

import { VaultsPage } from '@/components/workbench/VaultsPage';

import { B64_32, DEFAULT_PASSWORD, HEX_32, SAMPLE } from '../../kit/vault-generate/data';
import { CreateDialog, CreateFieldsDialog, EditFieldDialog, ProvideDialog } from '../../kit/vault-generate/dialogs';
import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { alone, avaultFixtures } from './mnemonic/fixtures';

const fixtures = avaultFixtures('0.1.11');
const W = 1000;
const phone = (title: string, body: ReactNode) => consolePage(<><VaultsPage />{alone(title, body)()}</>);
const gen = { kind: 'generated', recipe: DEFAULT_PASSWORD, value: SAMPLE.password } as const;
const prot = { kind: 'protected', recipe: DEFAULT_PASSWORD } as const;
const hexGen = { kind: 'generated', recipe: HEX_32, value: SAMPLE.hex, shown: true } as const;

const designFrames = frames([
  // ---- 1. Add a secret, single value -------------------------------------------------------
  { name: 'create-a', title: 'A · 添加 · 值来源「生成」· 标准档 · 默认密码', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="A" tier="standard" state={gen} />) },
  { name: 'create-b', title: 'B · 添加 · 值框里的骰子 · 标准档 · 点一下就填好', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="B" tier="standard" state={gen} />) },
  { name: 'create-b-adjust', title: 'B · 添加 · 展开「调整」改选项', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="B" tier="standard" state={{ ...gen, optionsOpen: true }} />) },
  { name: 'create-hex', title: 'A · 添加 · Hex 32 字节（应用自己的密钥，如会话签名）· 显示中', width: W, height: 1250, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="A" tier="standard" state={hexGen} name="SESSION_SECRET" />) },
  { name: 'create-base64url', title: 'A · 添加 · Base64url 32 字节', width: W, height: 1250, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="A" tier="standard" state={{ kind: 'generated', recipe: B64_32, value: SAMPLE.base64url, shown: true }} name="WEBHOOK_SECRET" />) },
  { name: 'create-b-typed', title: 'B · 添加 · 生成后自己改了 → 变回普通输入（chip 消失）', width: W, height: 1050, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="B" tier="standard" state={{ kind: 'typed', value: `${SAMPLE.password}x` }} />) },
  { name: 'create-protected-a', title: 'A · 添加 · 保护档：只选格式，保存时在安全窗口里生成', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="A" tier="protected" state={prot} />) },
  { name: 'create-protected-b', title: 'B · 添加 · 保护档', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="B" tier="protected" state={prot} />) },
  { name: 'create-tier-switched', title: 'A · 标准档生成后切到保护档：页面里的值丢弃，不带过去', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog variant="A" tier="protected" state={{ ...prot, switched: true }} />) },

  // ---- 2. Several fields, one row at a time ------------------------------------------------
  { name: 'fields-standard-a', title: 'A · 多字段 · 只生成 password 一行 · 标准档', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateFieldsDialog variant="A" tier="standard" />) },
  { name: 'fields-standard-b', title: 'B · 多字段 · 只生成 password 一行 · 标准档', width: W, height: 1200, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateFieldsDialog variant="B" tier="standard" />) },
  { name: 'fields-protected', title: 'A · 多字段 · 保护档', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateFieldsDialog variant="A" tier="protected" />) },

  // ---- 3. Edit a field ---------------------------------------------------------------------
  { name: 'edit-standard-a', title: 'A · 编辑 · 给 password 生成新值，点这一行「保存」才替换', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('编辑密钥', <EditFieldDialog variant="A" tier="standard" />) },
  { name: 'edit-standard-b', title: 'B · 编辑 · 同上，选项折叠', width: W, height: 1050, status: 'proposed', fixtures,
    render: alone('编辑密钥', <EditFieldDialog variant="B" tier="standard" />) },
  { name: 'edit-protected', title: 'B · 编辑 · 保护档：保存这一行时在安全窗口里生成', width: W, height: 1050, status: 'proposed', fixtures,
    render: alone('编辑密钥', <EditFieldDialog variant="B" tier="protected" />) },

  // ---- 4. The Agent's request card -----------------------------------------------------------
  { name: 'request-a', title: 'A · Agent 请求 · 它要求 Hex 32 字节，生成器按它预设', width: W, height: 1350, status: 'proposed', fixtures,
    render: alone('补充密钥', <ProvideDialog variant="A" tier="standard" state={{ kind: 'generated', recipe: HEX_32, value: SAMPLE.hex }} />) },
  { name: 'request-b', title: 'B · Agent 请求 · 同上', width: W, height: 1250, status: 'proposed', fixtures,
    render: alone('补充密钥', <ProvideDialog variant="B" tier="standard" state={{ kind: 'generated', recipe: HEX_32, value: SAMPLE.hex }} />) },
  { name: 'request-protected', title: 'A · Agent 请求 · 保护档', width: W, height: 1350, status: 'proposed', fixtures,
    render: alone('补充密钥', <ProvideDialog variant="A" tier="protected" state={{ kind: 'protected', recipe: HEX_32 }} />) },

  // ---- 5. Phone -----------------------------------------------------------------------------
  { name: 'create-a-phone', title: 'A · 添加 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog variant="A" tier="standard" state={gen} />) },
  { name: 'create-b-phone', title: 'B · 添加 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog variant="B" tier="standard" state={gen} />) },
  { name: 'request-a-phone', title: 'A · Agent 请求 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('补充密钥', <ProvideDialog variant="A" tier="standard" state={{ kind: 'generated', recipe: HEX_32, value: SAMPLE.hex }} />) },
  { name: 'edit-b-phone', title: 'B · 编辑 · 手机', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('编辑密钥', <EditFieldDialog variant="B" tier="standard" />) },
]);

export default designFrames;
