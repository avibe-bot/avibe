import type { ReactNode } from 'react';
import type { FrameSpec } from '../../frame';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { MobileLanguage, type MobileAccount, type MobileInstance, type MobileLicensesFixture, type MobileLoginFixture } from '../../kit/mobile-app';

export const account: MobileAccount = { name: 'Max', email: 'max@example.com' };
const codeSent: MobileLoginFixture = { step: 'code', email: account.email };
export const loginStates: { name: string; title: string; fixture: MobileLoginFixture }[] = [
  { name: 'default', title: '登录 / 注册 · 输入邮箱', fixture: { step: 'email', email: '' } },
  { name: 'email-invalid', title: '邮箱格式错误', fixture: { step: 'email', email: 'max@', error: 'email-invalid' } },
  { name: 'sending', title: '发送验证码中', fixture: { step: 'email', email: account.email, busy: 'sending' } },
  { name: 'email-rate-limited', title: '发送 · 请求过于频繁', fixture: { step: 'email', email: account.email, error: 'rate-limited' } },
  { name: 'email-network-error', title: '发送 · 网络错误', fixture: { step: 'email', email: account.email, error: 'network' } },
  { name: 'code-sent', title: '验证码已发送 · 可修改邮箱', fixture: codeSent },
  { name: 'code-partial', title: '验证码 · 输入中', fixture: { ...codeSent, code: '12' } },
  { name: 'code-ready', title: '已输入六位 · 可验证', fixture: { ...codeSent, code: '123456' } },
  { name: 'resending', title: '重新发送中', fixture: { ...codeSent, busy: 'resending' } },
  { name: 'resend-success', title: '重发成功 · 60 秒倒计时', fixture: { ...codeSent, cooldown: 60, resent: true } },
  { name: 'resend-failed', title: '重发失败 · 15 秒倒计时', fixture: { ...codeSent, cooldown: 15, error: 'resend-failed' } },
  { name: 'verifying', title: '验证中', fixture: { ...codeSent, code: '123456', busy: 'verifying' } },
  { name: 'code-invalid', title: '验证码错误 · 沿用合并提示', fixture: { ...codeSent, code: '123455', error: 'code-invalid' } },
  { name: 'code-expired', title: '验证码过期 · 沿用合并提示', fixture: { ...codeSent, code: '123456', error: 'code-expired' } },
  { name: 'rate-limited', title: '重发 · 请求过于频繁', fixture: { ...codeSent, cooldown: 15, error: 'rate-limited' } },
  { name: 'network-error', title: '验证 · 网络错误保留输入', fixture: { ...codeSent, code: '123456', error: 'network' } },
];
export const smallCodeFixture: MobileLoginFixture = { ...codeSent, email: 'alexandra.chen+research-and-product-planning@international.example.com', compact: true, cooldown: 15, error: 'resend-failed' };
export const personal: MobileInstance = { id: 'personal', name: 'Personal Workbench', host: 'personal-app.avibe.bot', online: true };
const lab: MobileInstance = { id: 'lab', name: 'Lab', host: 'lab-app.avibe.bot', online: true };
export const ownInstances = [personal, lab];
export const mixedInstances: MobileInstance[] = [
  personal,
  { id: 'team', name: 'Product team', host: 'team-app.avibe.bot', owner: 'Zhou Min', online: true },
  { id: 'weekend', name: 'Weekend lab', host: 'weekend-app.avibe.bot', online: false },
];
export const longInstances: MobileInstance[] = [
  { ...personal, name: 'Personal Workbench for research and product planning' },
  { ...mixedInstances[1], owner: 'Alexandra Chen · Product Research' },
];

/** Locale is explicit so both translations survive the exporter's id+theme key. */
export function bilingual(name: string, title: string, render: () => ReactNode, size: [number, number] = [390, 844]): FrameSpec[] {
  return (['zh', 'en'] as const).map(lang => ({
    name: `${name}-${lang}`, title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
    width: size[0], height: size[1], status: 'approved', themes: ['light', 'dark'],
    fixtures: {
      bots: [],
      api: { '/api/session': { remote: true, authenticated: true, email: account.email } },
      // Locale-specific ids must carry the same locale into shipped component strings,
      // even when the canvas itself is opened in the other language.
      copy: Object.fromEntries(['signedInAs', 'signOut', 'signingOut', 'logoAlt'].map(key => {
        const copy = (lang === 'en' ? en : zh).appShell[key as keyof typeof en.appShell] as string;
        return [`appShell.${key}`, { zh: copy, en: copy }];
      })),
    },
    render: () => <MobileLanguage lang={lang}>{render()}</MobileLanguage>,
  }));
}

const mitBody = 'Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.';
const uniffiCommit = '05b45b110f652d58d94c653992aeb3adc3a094f4';
const crate = (name: string, version: string) => `https://static.crates.io/crates/${name}/${name}-${version}.crate`;
/**
 * Shaped like the notices the iOS iroh build collects (packaging/iroh-apple, #3551): attohttpc and
 * nine uniffi-family crates under MPL-2.0, 330 packages in all. Versions and texts are illustrative;
 * the App renders the generated file.
 */
export const licensesFixture: MobileLicensesFixture = {
  count: 330,
  sources: [
    { name: 'attohttpc', version: '0.30.1', registry: crate('attohttpc', '0.30.1'), upstream: 'https://github.com/sbstp/attohttpc' },
    ...['uniffi', 'uniffi_bindgen', 'uniffi_build', 'uniffi_core', 'uniffi_internal_macros', 'uniffi_macros', 'uniffi_meta', 'uniffi_pipeline', 'uniffi_udl'].map(name => ({
      name, version: '0.31.2', registry: crate(name, '0.31.2'), upstream: `https://github.com/mozilla/uniffi-rs/tree/${uniffiCommit}`,
    })),
  ],
  licenses: [
    { name: 'aead', version: '0.5.2', license: 'MIT OR Apache-2.0', text: mitBody },
    { name: 'anyhow', version: '1.0.98', license: 'MIT OR Apache-2.0', text: mitBody },
    { name: 'attohttpc', version: '0.30.1', license: 'MPL-2.0', text: 'Mozilla Public License Version 2.0\n==================================\n\n1. Definitions\n--------------\n\n1.1. "Contributor"\nmeans each individual or legal entity that creates, contributes to the creation of, or owns Covered Software.\n\n1.2. "Contributor Version"\nmeans the combination of the Contributions of others (if any) used by a Contributor and that particular Contributor\'s Contribution.' },
  ],
};
