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

  it('lets a very long address wrap rather than overflow a phone', async () => {
    await i18n.changeLanguage('en');
    const long = `${'a'.repeat(60)}-app.avibe.bot`;
    const { container } = render(<p><KeptTail text={`Open ${long} now.`} /></p>);
    expect(kept(container).some((each) => each.includes(long))).toBe(false);
    expect(container.textContent).toBe(`Open ${long} now.`);
  });
});
