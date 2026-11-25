import en from '../en.json';
import zh from '../zh.json';
import accountEn from './en.json';
import accountZh from './zh.json';

/**
 * The Desktop Account page's own copy (`desktopAccount.*`): only the bundled account document reads
 * it, so the Web UI's per-language chunk (./app.ts), which every first screen downloads, leaves it
 * out (scripts/check-first-load-budget.mjs). `../index.ts` adds it for the account document, the
 * design canvas and tests; `desktopAccountCopy.test.ts` keeps the Web UI from asking for it.
 */
export const desktopAccountCopy = { en: accountEn, zh: accountZh } as const;

/** Each language's whole copy as the account document sees it. */
export const desktopEn = { ...en, ...accountEn };
export const desktopZh = { ...zh, ...accountZh };
