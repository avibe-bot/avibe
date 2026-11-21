/** @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { KeptTail } from './kept-tail';

afterEach(cleanup);

const tail = (container: HTMLElement) => Array.from(container.querySelectorAll('span.whitespace-nowrap')).map((each) => each.textContent);

describe('KeptTail', () => {
  it('keeps at least eight Chinese characters together at the end, cut at a word boundary', () => {
    const { container } = render(<p><KeptTail text="已同步的健康数据会被删除，要在 iPhone 上重新连接健康。" /></p>);
    expect(container.textContent).toBe('已同步的健康数据会被删除，要在 iPhone 上重新连接健康。');
    const kept = tail(container).at(-1) ?? '';
    expect(kept.length).toBeGreaterThanOrEqual(8);
    expect(kept.endsWith('连接健康。')).toBe(true);
  });

  it('keeps the last three English words together and never splits an address', () => {
    const { container } = render(<p><KeptTail text="In that browser, open max-app.avibe.bot and choose Sign out in the account menu." /></p>);
    expect(tail(container)).toContain('max-app.avibe.bot');
    expect(tail(container).at(-1)).toBe('the account menu.');
  });
});
