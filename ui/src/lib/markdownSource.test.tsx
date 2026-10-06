// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Markdown } from '@/components/ui/markdown';
import type { MentionReference } from '@/lib/mentions';
import { selectedMarkdown, wholeMarkdownRange } from './markdownSource';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(cleanup);

const DOC = [
  '## Plan **now**',
  '',
  'Intro with **bold text** and a [link](https://example.com/a) here.',
  '',
  '- first item',
  '- second `code` item',
  '',
  '```ts',
  'const a = 1;',
  'const b = 2;',
  '```',
  '',
  '| key | val |',
  '|---|---|',
  '| x | yes |',
  '',
  '这是**重点**内容。见[文档][docs]。',
  '',
  '[docs]: https://example.com/docs',
].join('\n');

type Edge = [needle: string, side: 'before' | 'after', textNode?: number];

// The DOM point just before or after `needle` in the first (or `textNode`-th)
// rendered text node that holds it.
function pointAt(root: Element, [needle, side, textNode = 0]: Edge): [Node, number] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let seen = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const at = node.textContent!.indexOf(needle);
    if (at >= 0 && seen++ === textNode) return [node, side === 'before' ? at : at + needle.length];
  }
  throw new Error(`no text node holds ${needle}`);
}

function rangeOver(container: Element, start: Edge, end: Edge): Range {
  const range = document.createRange();
  range.setStart(...pointAt(container, start));
  range.setEnd(...pointAt(container, end));
  return range;
}

function renderDoc(content = DOC, references?: MentionReference[]) {
  const { container } = render(<Markdown content={content} references={references} />);
  return container;
}

describe('selectedMarkdown', () => {
  it('copies a whole bubble as exactly the Markdown it was rendered from', () => {
    const container = renderDoc();
    const range = document.createRange();
    range.selectNodeContents(container.querySelector('.vr-markdown')!);

    expect(selectedMarkdown(range, container)).toBe(DOC);
  });

  it.each<[string, Edge, Edge, string]>([
    ['a cut inside one paragraph', ['with', 'before'], ['and', 'after'], 'with **bold text** and'],
    ['a cut inside one emphasis run', ['old', 'before'], ['tex', 'after'], 'old tex'],
    ['a selection leaving emphasis', ['text', 'before'], ['and', 'after'], '**bold text** and'],
    ['a selection leaving a link', ['nk', 'before'], ['here', 'after'], '[link](https://example.com/a) here'],
    ['a selection leaving CJK emphasis', ['点', 'before'], ['内容', 'after'], '**重点**内容'],
    ['a cut from a paragraph into a list', ['here', 'before'], ['first', 'after'], 'here.\n\n- first'],
    ['whole list items', ['first', 'before'], [' item', 'after', 1], '- first item\n- second `code` item'],
    ['a heading selected from its start', ['Plan', 'before'], ['Intro', 'after'], '## Plan **now**\n\nIntro'],
    ['a cut inside a code block', ['a = 1', 'before'], ['a = 1', 'after'], 'a = 1'],
    [
      'a selection leaving a code block into a table',
      ['const b', 'before'],
      ['key', 'after'],
      '```ts\nconst a = 1;\nconst b = 2;\n```\n\n| key | val |\n|---|---|\n| x | yes |',
    ],
    ['a cut inside one table cell', ['es', 'before'], ['es', 'after'], 'es'],
    ['a selection across two cells of one row', ['ey', 'before'], ['va', 'after'], '| key | val |\n|---|---|\n| x | yes |'],
    ['a selection across two rows', ['x', 'before', 1], ['ye', 'after'], '| key | val |\n|---|---|\n| x | yes |'],
  ])('copies %s', (_case, start, end, expected) => {
    const container = renderDoc();
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it('copies a mention chip as the marker that was typed', () => {
    const content = 'ask @<claude> to review';
    const container = renderDoc(content, [{ kind: 'agent', name: 'claude' }]);
    const whole = document.createRange();
    whole.selectNodeContents(container);

    expect(selectedMarkdown(whole, container)).toBe(content);
    expect(selectedMarkdown(rangeOver(container, ['aude', 'before'], ['review', 'after']), container))
      .toBe('@<claude> to review');
  });

  it('copies each bubble a selection crosses, separated by a blank line', () => {
    const { container } = render(
      <>
        <Markdown content="first **bubble** text" />
        <span>12:00</span>
        <Markdown content={'- second\n- bubble'} />
      </>,
    );
    const range = rangeOver(container, ['bubble', 'before'], ['second', 'after']);

    expect(selectedMarkdown(range, container)).toBe('**bubble** text\n\n- second');
  });

  it('reports no Markdown for a selection outside every bubble', () => {
    const { container } = render(
      <>
        <span>12:00</span>
        <Markdown content="body" />
      </>,
    );
    expect(selectedMarkdown(rangeOver(container, ['12', 'before'], ['00', 'after']), container)).toBeNull();
  });
});

describe('wholeMarkdownRange', () => {
  it('widens a selection to every bubble it reaches', () => {
    const { container } = render(
      <>
        <Markdown content="first **bubble** text" />
        <span>12:00</span>
        <Markdown content={'- second\n- bubble'} />
        <Markdown content="untouched" />
      </>,
    );
    const whole = wholeMarkdownRange(rangeOver(container, ['bubble', 'before'], ['second', 'after']), container)!;

    expect(whole.toString()).toBe(`first bubble text12:00${container.querySelectorAll('.vr-markdown')[1].textContent}`);
    expect(selectedMarkdown(whole, container)).toBe('first **bubble** text\n\n- second\n- bubble');
  });
});
