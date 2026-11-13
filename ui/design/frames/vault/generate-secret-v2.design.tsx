// Generate a password or application secret inside Vaults, version 2 (avibe#3338; owner 2026-11-13
// 20:58 and 21:08): proposed. The generator is attached to the value field, as in 1Password: a dice
// control at the field's end reads 「生成随机密码」 while the field has focus (on a phone, while it
// is empty), and opens a popup anchored to the field (a bottom sheet on a phone) with the value,
// regenerate, the type, a length slider, switches, and 取消 / 使用. 使用 fills the field; the
// dialog's own Save saves. Protected never shows a value: the popup sets the format and the secure
// window generates at save. On Standard an Agent may also generate a new secret itself, stating
// only the format; it gets back the value-free row. Dialogs are drawn complete, field for field as
// shipped (kit/vault-generate). Version 1 stays in generate-secret.design.tsx for comparison.
import type { ReactNode } from 'react';

import { VaultsPage } from '@/components/workbench/VaultsPage';

import { BASE64_32, HEX32, PASSWORD_24 } from '../../kit/vault-generate/data';
import { AgentRunCard, ProvenanceLine, VaultsListWithAgent } from '../../kit/vault-generate/agent';
import { CreateDialog2, CreateFieldsDialog2, EditFieldDialog2, ProvideDialog2 } from '../../kit/vault-generate/v2';
import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { alone, avaultFixtures, onCanvas } from './mnemonic/fixtures';

const fixtures = avaultFixtures('0.1.11');
const W = 1000;
const phone = (title: string, body: ReactNode) => consolePage(<><VaultsPage />{alone(title, body)()}</>);
const focusValue = ['input[type="password"]'];
const gen = { kind: 'generated', gen: PASSWORD_24 } as const;
const prot = { kind: 'protected', gen: PASSWORD_24 } as const;
const empty = { kind: 'empty' } as const;

const designFrames = frames([
  // ---- Add a secret, one value ---------------------------------------------------------------
  { name: 'focus', title: '添加 · 光标在值框里：出现「生成随机密码」', width: W, height: 1150, status: 'proposed', fixtures, click: focusValue,
    render: alone('添加密钥', <CreateDialog2 tier="standard" state={empty} />) },
  { name: 'popup', title: '添加 · 点开：浮层贴着值框，默认 24 位密码', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog2 tier="standard" state={empty} open />) },
  { name: 'popup-hex', title: '添加 · 浮层 · Hex 32 字节', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog2 tier="standard" state={empty} open gen={HEX32} name="SESSION_SECRET" />) },
  { name: 'popup-base64', title: '添加 · 浮层 · Base64 32 字节（= openssl rand -base64 32）', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog2 tier="standard" state={empty} open gen={BASE64_32} name="WEBHOOK_SECRET" />) },
  { name: 'used', title: '添加 · 点「使用」后：值填进框里，标「已生成 · 未保存」', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog2 tier="standard" state={gen} />) },
  { name: 'protected-popup', title: '添加 · 保护档浮层：只有设置，没有值', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog2 tier="protected" state={empty} open />) },
  { name: 'protected-used', title: '添加 · 保护档「使用」后：框里写明保存时生成', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateDialog2 tier="protected" state={prot} />) },

  // ---- Several fields --------------------------------------------------------------------------
  { name: 'fields-popup', title: '多字段 · 在 password 这一行打开', width: W, height: 1250, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateFieldsDialog2 tier="standard" open />) },
  { name: 'fields-used', title: '多字段 · 只有 password 这一行是生成的', width: W, height: 1250, status: 'proposed', fixtures,
    render: alone('添加密钥', <CreateFieldsDialog2 tier="standard" used />) },

  // ---- Edit a field ------------------------------------------------------------------------------
  { name: 'edit-popup', title: '编辑 · 给 password 生成新值', width: W, height: 1100, status: 'proposed', fixtures,
    render: alone('编辑密钥', <EditFieldDialog2 tier="standard" open />) },
  { name: 'edit-used', title: '编辑 · 「使用」后，点这一行「保存」才替换', width: W, height: 1100, status: 'proposed', fixtures,
    render: alone('编辑密钥', <EditFieldDialog2 tier="standard" used />) },
  { name: 'edit-protected-used', title: '编辑 · 保护档：保存这一行时在安全窗口里生成', width: W, height: 1100, status: 'proposed', fixtures,
    render: alone('编辑密钥', <EditFieldDialog2 tier="protected" used />) },

  // ---- The Agent's request ---------------------------------------------------------------------------
  { name: 'request-popup', title: 'Agent 请求 · 浮层按它的建议预设 Hex 32 字节', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('补充密钥', <ProvideDialog2 tier="standard" state={empty} open />) },
  { name: 'request-protected-popup', title: 'Agent 请求 · 保护档', width: W, height: 1300, status: 'proposed', fixtures,
    render: alone('补充密钥', <ProvideDialog2 tier="protected" state={empty} open />) },

  // ---- The Agent generates on Standard by itself -------------------------------------------------------
  { name: 'agent-run', title: 'Agent 自己生成（标准档）：它运行的命令和拿回的全部内容', width: 760, height: 560, status: 'proposed', fixtures,
    render: onCanvas(<AgentRunCard />, 700) },
  { name: 'agent-list', title: 'Vaults 列表：Agent 生成的密钥带标记和会话', width: W, height: 420, status: 'proposed', fixtures,
    render: onCanvas(<VaultsListWithAgent />, 920) },
  { name: 'agent-edit', title: '打开它：来自哪个会话、打开会话、删除', width: W, height: 1150, status: 'proposed', fixtures,
    render: alone('编辑密钥', <EditFieldDialog2 tier="standard" provenance={<ProvenanceLine />} />) },

  // ---- Phone -----------------------------------------------------------------------------------------
  { name: 'focus-phone', title: '手机 · 值框为空时一直显示「生成随机密码」', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog2 tier="standard" state={empty} />) },
  { name: 'sheet-phone', title: '手机 · 底部弹层', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog2 tier="standard" state={empty} open />) },
  { name: 'used-phone', title: '手机 · 「使用」后', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog2 tier="standard" state={gen} />) },
  { name: 'request-sheet-phone', title: '手机 · Agent 请求', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('补充密钥', <ProvideDialog2 tier="standard" state={empty} open />) },
  { name: 'protected-sheet-phone', title: '手机 · 保护档', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog2 tier="protected" state={empty} open />) },
  { name: 'edit-sheet-phone', title: '手机 · 编辑字段', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('编辑密钥', <EditFieldDialog2 tier="standard" open />) },
  { name: 'agent-list-phone', title: '手机 · 列表里的 Agent 标记', width: 390, height: 600, status: 'proposed', fixtures,
    render: onCanvas(<VaultsListWithAgent />, 360) },
]);

export default designFrames;
