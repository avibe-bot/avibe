// The generator attached to a static value (avibe#3338; owner-approved design 2026-11-14): 「随机密码」
// at the value field fills it at once and opens the settings anchored to it; every change regenerates
// and the field follows; closing keeps the value, marked 「已生成 · 未保存」, until the dialog's Save.
// Both tiers alike. Drawn with the shipped dialog; the values are random on every render.
import type { ReactNode } from 'react';

import { VaultSecretDialog } from '@/components/ui/vault-secret-dialog';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { provisionRequest, vaultFixtures } from './fixtures';

const noop = () => {};
const fixtures = vaultFixtures();
const create = () => <VaultSecretDialog open onOpenChange={noop} onCreated={noop} />;
const suggested = { ...provisionRequest, secret_name: 'SESSION_SECRET', card: { ...provisionRequest.card, spec: { description: 'Web session signing key', generate: { type: 'hex', bytes: 32 } } } };
const provide = () => <VaultSecretDialog open onOpenChange={noop} request={suggested} onCancel={noop} onDeny={noop} onCreated={noop} />;
const page = (dialog: () => ReactNode) => consolePage(<><VaultsPage />{dialog()}</>);

const generate = 'role=button[name="{t:vaults.generate.password}"]';
const generateKey = 'role=button[name="{t:vaults.generate.key}"]';
const protectedTier = 'role=button[name*="{t:vaults.dialog.protectedProtection}"]';
const login = ['role=button[name="{t:vaults.fields.useTemplate}"]', 'role=menuitem[name="{t:vaults.fields.templateLogin}"]'];

const designFrames = frames([
  { name: 'focus', title: '光标进入值框：出现「随机密码」', width: 1000, height: 1000, status: 'shipped', fixtures,
    click: ['input[type="password"]'], render: create },
  { name: 'open', title: '点一下：值直接进框（明文），设置浮层贴在下面', width: 1000, height: 1050, status: 'shipped', fixtures,
    click: [generate], render: create },
  { name: 'hex', title: '切到 Hex：框里立刻换成 64 位 Hex，按钮叫「随机密钥」', width: 1000, height: 1050, status: 'shipped', fixtures,
    click: [generate, 'role=radio[name="{t:vaults.generate.typeHex}"]'], render: create },
  { name: 'protected', title: '保护档：和标准档完全一样', width: 1000, height: 1050, status: 'shipped', fixtures,
    click: [protectedTier, generate], render: create },
  { name: 'fields', title: '多字段：每一行自己的「随机密码」', width: 1000, height: 1200, status: 'shipped', fixtures,
    click: [...login, `${generate} >> nth=1`], render: create },
  { name: 'request', title: 'Agent 请求：按它的建议（Hex 32 字节）预设', width: 1000, height: 1200, status: 'shipped', fixtures,
    click: [generateKey], render: provide },
  { name: 'open-phone', title: '手机：同一个贴着值框的浮层', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures,
    click: [generate], render: page(create) },
  { name: 'request-phone', title: '手机：Agent 请求', width: 390, height: 844, status: 'shipped', route: '/vaults', fixtures,
    click: [generateKey], render: page(provide) },
]);

export default designFrames;
