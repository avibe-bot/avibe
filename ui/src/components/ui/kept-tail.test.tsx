/** @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import i18n from '../../i18n';
import { KeptTail } from './kept-tail';

afterEach(cleanup);

const kept = (container: HTMLElement) => Array.from(container.querySelectorAll('span.whitespace-nowrap')).map((each) => each.textContent ?? '');

describe('KeptTail', () => {
  it('keeps at least eight Chinese characters together at the end, cut at a word boundary', async () => {
    await i18n.changeLanguage('zh');
    const { container } = render(<p><KeptTail text="已同步的健康数据会被删除，要在 iPhone 上重新连接健康。" /></p>);
    expect(container.textContent).toBe('已同步的健康数据会被删除，要在 iPhone 上重新连接健康。');
    const tail = kept(container).at(-1) ?? '';
    expect(tail.length).toBeGreaterThanOrEqual(8);
    expect(tail.endsWith('连接健康。')).toBe(true);
  });

  it('keeps the last three English words together and never splits an address', async () => {
    await i18n.changeLanguage('en');
    const { container } = render(<p><KeptTail text="In that browser, open max-app.avibe.bot and choose Sign out in the account menu." /></p>);
    expect(kept(container)).toContain('max-app.avibe.bot');
    expect(kept(container).at(-1)).toBe('the account menu.');
  });

  it('moves a cut that would fall inside a hyphenated address to its start', async () => {
    await i18n.changeLanguage('zh');
    const { container } = render(<p><KeptTail text="所有设备都已退出，要在新地址重新登录；max-app.avibe.bot 已作废。" /></p>);
    expect(container.textContent).toBe('所有设备都已退出，要在新地址重新登录；max-app.avibe.bot 已作废。');
    expect(kept(container).at(-1)).toBe('max-app.avibe.bot 已作废。');
    await i18n.changeLanguage('en');
    const english = render(<p><KeptTail text="Every device was signed out; max-app.avibe.bot is void." /></p>);
    expect(kept(english.container).at(-1)).toBe('max-app.avibe.bot is void.');
  });

  it('follows the page language, not a Chinese name inside English copy', async () => {
    await i18n.changeLanguage('en');
    const { container } = render(<p><KeptTail text="Sign out Max 的 iPhone and keep the data for you." /></p>);
    expect(kept(container).at(-1)).toBe('data for you.');
  });

  it('lets a body paragraph fill its lines, keeping only its last few characters together', async () => {
    await i18n.changeLanguage('zh');
    const body = '这台电脑会重新向 avibe.bot 登记，用你接下来在 avibe.bot 登录的账号。完成后，旧的设备密钥作废，所有远程设备都会退出、要重新登录。';
    const { container } = render(<p><KeptTail text={body} /></p>);
    expect(container.textContent).toBe(body);
    // The addresses stay whole; the tail is too short to stop the line before it visibly early.
    expect(kept(container)).toEqual(['avibe.bot', 'avibe.bot', '重新登录。']);
    const hint = '只有这一次：去 avibe.bot 起个名字、领取域名，然后自动回到「设备」页，在那里添加手机。';
    expect(kept(render(<p><KeptTail text={hint} /></p>).container).at(-1)).toBe('添加手机。');
    await i18n.changeLanguage('en');
    const english = 'The health data this device synced here is deleted once this Avibe\'s owner is confirmed; if that cannot be confirmed it is kept.';
    expect(kept(render(<p><KeptTail text={english} /></p>).container)).toEqual(['is kept.']);
  });

  it('keeps no tail in a body paragraph when an address would make it long', async () => {
    await i18n.changeLanguage('zh');
    const body = '所有设备都已退出，要在新地址重新登录，这台电脑上的页面不受影响；从现在起请在浏览器里打开 max-app.avibe.bot。';
    const { container } = render(<p><KeptTail text={body} /></p>);
    expect(container.textContent).toBe(body);
    // The address is whole, and nothing more is tied to it.
    expect(kept(container)).toEqual(['max-app.avibe.bot']);
  });

  it('lets a very long address wrap rather than overflow a phone', async () => {
    await i18n.changeLanguage('en');
    const long = `${'a'.repeat(60)}-app.avibe.bot`;
    const { container } = render(<p><KeptTail text={`Open ${long} now.`} /></p>);
    expect(kept(container).some((each) => each.includes(long))).toBe(false);
    expect(container.textContent).toBe(`Open ${long} now.`);
  });
  it('fills the line in a full-width row: only a body paragraph\'s short tail is kept', () => {
    const text = '这个选择只管这台设备。选了之后连不上，App 会自己改回「自动」。';
    const short = render(<p><KeptTail text={text} /></p>).container;
    const kept = (container: HTMLElement) => container.querySelector('span.whitespace-nowrap')?.textContent ?? '';
    const filled = render(<p><KeptTail fill text={text} /></p>).container;
    expect(kept(short).length).toBeGreaterThan(kept(filled).length);
    expect(Array.from(kept(filled)).length).toBeLessThanOrEqual(8);
  });
});
