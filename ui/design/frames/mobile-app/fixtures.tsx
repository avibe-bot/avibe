import type { ReactNode } from 'react';
import type { FrameSpec } from '../../frame';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { MobileLanguage, type MobileAccount, type MobileInstance, type MobileLoginFixture } from '../../kit/mobile-app';

export const account: MobileAccount = { name: 'Max', email: 'max@example.com' };
const codeSent: MobileLoginFixture = { step: 'code', email: account.email };
export const loginStates: { name: string; title: string; fixture: MobileLoginFixture }[] = [
  { name: 'default', title: '登录 / 注册 · 输入邮箱', fixture: { step: 'email', email: '' } },
  { name: 'email-invalid', title: '邮箱格式错误', fixture: { step: 'email', email: 'max@', error: 'email-invalid' } },
  { name: 'sending', title: '发送验证码中', fixture: { step: 'email', email: account.email, busy: 'sending' } },
  { name: 'email-rate-limited', title: '发送 · 请求过于频繁', fixture: { step: 'email', email: account.email, error: 'rate-limited' } },
  { name: 'email-network-error', title: '发送 · 网络错误', fixture: { step: 'email', email: account.email, error: 'network' } },
  { name: 'code-sent', title: '验证码已发送 · 可修改邮箱', fixture: codeSent },
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
export const smallCodeFixture: MobileLoginFixture = { ...codeSent, email: 'alexandra.chen+research@example.com', cooldown: 15, error: 'resend-failed' };
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
    width: size[0], height: size[1], status: 'proposed', themes: ['light', 'dark'],
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
