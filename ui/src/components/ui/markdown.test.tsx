/* @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Markdown } from './markdown';

afterEach(cleanup);

describe('Markdown emphasis', () => {
  it.each([
    ['', 'p'],
    ['1. ', 'ol > li'],
    ['- ', 'ul > li'],
    ['> ', 'blockquote > p'],
    ['### ', 'h3'],
  ])('renders CJK punctuation boundaries inside %j blocks', (prefix, selector) => {
    const bold = 'A 不再出现在后续请求里，线程 ID 和聊天记录不变。';
    const continuation = '源码允许冷恢复时优先采用传入的基础指令。';
    const { container } = render(
      <Markdown content={`${prefix}**${bold}**${continuation}`} />,
    );
    const block = container.querySelector(selector);

    expect(block?.querySelector('strong')?.textContent).toBe(bold);
    expect(block?.textContent).toBe(bold + continuation);
  });

  it.each([false, true])('keeps adjacent CJK emphasis with softBreaks=%s', (softBreaks) => {
    const { container } = render(
      <Markdown
        content={'前文**「重点」**后文**结论。**继续\n下一行'}
        softBreaks={softBreaks}
        interactive={false}
      />,
    );

    expect(Array.from(container.querySelectorAll('strong'), (node) => node.textContent))
      .toEqual(['「重点」', '结论。']);
    expect(container.querySelectorAll('br')).toHaveLength(softBreaks ? 1 : 0);
  });

  it('preserves inline structure inside CJK strong emphasis', () => {
    const { container } = render(
      <Markdown content={'1. **保留 `Codex` 和[链接](https://example.com)。**后文'} />,
    );
    const strong = container.querySelector('li > strong');

    expect(strong?.textContent).toBe('保留 Codex 和链接。');
    expect(strong?.querySelector('code')?.textContent).toBe('Codex');
    expect(strong?.querySelector('a')?.getAttribute('href')).toBe('https://example.com');
    expect(container.querySelector('li')?.textContent).toBe('保留 Codex 和链接。后文');
  });

  it.each([
    ['`**中文。**后文`', 'code', '**中文。**后文'],
    ['```md\n**中文。**后文\n```', 'pre > code', '**中文。**后文\n'],
    ['    **中文。**后文', 'pre > code', '**中文。**后文\n'],
    [String.raw`\*\*中文。\*\*后文`, 'p', '**中文。**后文'],
    ['&#42;&#42;中文。&#42;&#42;后文', 'p', '**中文。**后文'],
    ['**中文。', 'p', '**中文。'],
    ['**中文。 **后文', 'p', '**中文。 **后文'],
  ])('preserves literal markers in %j', (content, selector, expected) => {
    const { container } = render(<Markdown content={content} interactive={false} />);

    expect(container.querySelector('strong')).toBeNull();
    expect(container.querySelector(selector)?.textContent).toBe(expected);
  });

  it('preserves standard emphasis and GFM rendering', () => {
    const { container } = render(
      <Markdown content={'- [x] **Bold** and *italic* and ~~deleted~~\n\n| A | B |\n| - | - |\n| one | two |'} />,
    );

    expect(container.querySelector('strong')?.textContent).toBe('Bold');
    expect(container.querySelector('em')?.textContent).toBe('italic');
    expect(container.querySelector('del')?.textContent).toBe('deleted');
    expect(container.querySelector<HTMLInputElement>('input')?.checked).toBe(true);
    expect(container.querySelectorAll('td')).toHaveLength(2);
  });
});
