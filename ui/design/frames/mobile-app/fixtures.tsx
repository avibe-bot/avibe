import type { ReactNode } from 'react';
import type { FrameSpec } from '../../frame';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { MobileLanguage, type MobileAccount, type MobileInstance } from '../../kit/mobile-app';

export const account: MobileAccount = { name: 'Max', email: 'max@example.com' };
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
