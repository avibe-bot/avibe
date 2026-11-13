// Generate a password or application secret inside Vaults, version 3 (avibe#3338; owner 2026-11-14
// 00:27): proposed. Live, as in 1Password: 「随机密码」 at the value field puts a value straight
// into the field and opens the settings anchored to it (on a phone too, no bottom sheet); every
// change regenerates and the field, in clear while the popup is open, follows at once. No 使用 /
// 取消: closing the popup keeps the value. No Save per row: the dialog's Save commits every field.
// The slider is a new kit component (kit/slider). Protected shows settings only. The Agent path is
// version 2's. Versions 1 and 2 stay in generate-secret(-v2).design.tsx for comparison.
import type { ReactNode } from 'react';

import { VaultsPage } from '@/components/workbench/VaultsPage';

import { BASE64_32, HEX32, PASSWORD_24, PASSWORD_40 } from '../../kit/vault-generate/data';
import { ProvenanceLine } from '../../kit/vault-generate/agent';
import { CreateDialog3, CreateFieldsDialog3, EditDialog3, ProvideDialog3, SliderSheet } from '../../kit/vault-generate/v3';
import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { alone, avaultFixtures, onCanvas } from './mnemonic/fixtures';

const fixtures = avaultFixtures('0.1.11');
const W = 1000;
const phone = (title: string, body: ReactNode) => consolePage(<><VaultsPage />{alone(title, body)()}</>);
const open = (gen = PASSWORD_24) => ({ kind: 'open', gen } as const);
const generated = { kind: 'generated', gen: PASSWORD_24 } as const;
const empty = { kind: 'empty' } as const;

const designFrames = frames([
  { name: 'slider', title: '新组件 · 滑动条的各个状态，和开关、分段控件放在一起', width: 760, height: 620, status: 'proposed', reducedMotion: true, fixtures,
    render: onCanvas(<SliderSheet />, 680) },

  // ---- Add a secret, one value ---------------------------------------------------------------
  { name: 'focus', title: '光标进入值框：出现「随机密码」', width: W, height: 1150, status: 'proposed', reducedMotion: true, fixtures, click: ['input[type="password"]'],
    render: alone('添加密钥', <CreateDialog3 tier="standard" state={empty} />) },
  { name: 'open', title: '点「随机密码」：值直接进框（明文），设置浮层贴在下面', width: W, height: 1150, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateDialog3 tier="standard" state={open()} />) },
  { name: 'drag', title: '拖到 40：框里的值实时变长', width: W, height: 1150, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateDialog3 tier="standard" state={{ kind: 'open', gen: PASSWORD_40, slider: 'pressed' }} />) },
  { name: 'open-hex', title: '切到 Hex：框里立刻换成 64 位 Hex，按钮叫「随机密钥」', width: W, height: 1100, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateDialog3 tier="standard" state={open(HEX32)} name="SESSION_SECRET" />) },
  { name: 'open-base64', title: '切到 Base64', width: W, height: 1100, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateDialog3 tier="standard" state={open(BASE64_32)} name="WEBHOOK_SECRET" />) },
  { name: 'closed', title: '点外面收起：值留在框里，重新遮住，标「已生成 · 未保存」', width: W, height: 1100, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateDialog3 tier="standard" state={generated} />) },
  { name: 'protected-open', title: '保护档：浮层只有设置，框里写明保存时生成', width: W, height: 1150, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateDialog3 tier="protected" state={{ kind: 'protected-open', gen: PASSWORD_24 }} />) },
  { name: 'protected-closed', title: '保护档 · 收起后', width: W, height: 1100, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateDialog3 tier="protected" state={{ kind: 'protected', gen: PASSWORD_24 }} />) },

  // ---- Several fields and edit: no Save per row ------------------------------------------------------
  { name: 'fields-open', title: '多字段 · 在 password 行打开', width: W, height: 1250, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateFieldsDialog3 tier="standard" state={open()} />) },
  { name: 'fields-closed', title: '多字段 · 收起后（行上没有保存按钮）', width: W, height: 1200, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('添加密钥', <CreateFieldsDialog3 tier="standard" state={generated} />) },
  { name: 'edit-open', title: '编辑 · 给 password 生成新值', width: W, height: 1150, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('编辑密钥', <EditDialog3 tier="standard" state={open()} />) },
  { name: 'edit-closed', title: '编辑 · 收起后：点底部「保存」才一起生效', width: W, height: 1100, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('编辑密钥', <EditDialog3 tier="standard" state={generated} />) },
  { name: 'edit-protected', title: '编辑 · 保护档：保存时在安全窗口里生成', width: W, height: 1100, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('编辑密钥', <EditDialog3 tier="protected" state={{ kind: 'protected', gen: PASSWORD_24 }} />) },

  // ---- The Agent's request ---------------------------------------------------------------------------
  { name: 'request-open', title: 'Agent 请求 · 按建议生成 Hex 32 字节', width: W, height: 1300, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('补充密钥', <ProvideDialog3 tier="standard" state={open(HEX32)} />) },
  { name: 'request-protected', title: 'Agent 请求 · 保护档', width: W, height: 1300, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('补充密钥', <ProvideDialog3 tier="protected" state={{ kind: 'protected-open', gen: HEX32 }} />) },

  // ---- The Agent generates on Standard by itself (version 2, unchanged but for the edit dialog) -----
  { name: 'agent-edit', title: 'Agent 生成的密钥 · 打开它（编辑框按第 3 版）', width: W, height: 1150, status: 'proposed', reducedMotion: true, fixtures,
    render: alone('编辑密钥', <EditDialog3 tier="standard" state={empty} provenance={<ProvenanceLine />} />) },

  // ---- Phone: the same anchored popup ----------------------------------------------------------------
  { name: 'focus-phone', title: '手机 · 值框为空时就显示「随机密码」', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog3 tier="standard" state={empty} inline />) },
  { name: 'open-phone', title: '手机 · 浮层贴在值框下面，值框一直看得见', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog3 tier="standard" state={open()} inline />) },
  { name: 'drag-phone', title: '手机 · 拖动时框里的值跟着变', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog3 tier="standard" state={{ kind: 'open', gen: PASSWORD_40, slider: 'pressed' }} inline />) },
  { name: 'closed-phone', title: '手机 · 收起后', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog3 tier="standard" state={generated} inline />) },
  { name: 'request-phone', title: '手机 · Agent 请求', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('补充密钥', <ProvideDialog3 tier="standard" state={open(HEX32)} inline />) },
  { name: 'protected-phone', title: '手机 · 保护档', width: 390, height: 844, status: 'proposed', route: '/vaults', fixtures,
    render: phone('添加密钥', <CreateDialog3 tier="protected" state={{ kind: 'protected-open', gen: PASSWORD_24 }} inline />) },
]);

export default designFrames;
