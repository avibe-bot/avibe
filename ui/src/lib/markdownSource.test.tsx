// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Markdown } from '@/components/ui/markdown';
import { bindCitations, bodyDigest } from '@/lib/citations';
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
  it.each([
    ['the document above', DOC],
    // Whitespace at either end, and the two spaces that make a hard break, are
    // part of what was written.
    ['source with edge whitespace and a hard break', '\n\nfirst line  \nsecond line\n'],
  ])('copies a whole bubble of %s byte for byte', (_case, content) => {
    const container = renderDoc(content);
    const range = document.createRange();
    range.selectNodeContents(container.querySelector('.vr-markdown')!);

    expect(selectedMarkdown(range, container)).toBe(content);
  });

  it.each<[string, Edge, Edge, string]>([
    ['a cut inside one paragraph', ['with', 'before'], ['and', 'after'], 'with **bold text** and'],
    ['a cut inside one emphasis run', ['old', 'before'], ['tex', 'after'], 'old tex'],
    ['a selection leaving emphasis', ['text', 'before'], ['and', 'after'], '**bold text** and'],
    ['a selection leaving a link', ['nk', 'before'], ['here', 'after'], '[link](https://example.com/a) here'],
    // A link renders its label, not its source: a cut inside it takes it whole.
    ['a cut inside a link label', ['in', 'before'], ['in', 'after'], '[link](https://example.com/a)'],
    ['a selection leaving CJK emphasis', ['点', 'before'], ['内容', 'after'], '**重点**内容'],
    // A selection that crosses blocks copies each block it touches, whole.
    [
      'a selection from a paragraph into a list',
      ['here', 'before'],
      ['first', 'after'],
      'Intro with **bold text** and a [link](https://example.com/a) here.\n\n- first item',
    ],
    ['whole list items', ['first', 'before'], [' item', 'after', 1], '- first item\n- second `code` item'],
    ['a whole heading', ['Plan', 'before'], ['now', 'after'], '## Plan **now**'],
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
    // A reference link keeps working only with its definition, rendered nowhere near it.
    ['a cut holding a reference link', ['内容', 'before'], ['文档', 'after'], '内容。见[文档][docs]\n\n[docs]: https://example.com/docs'],
  ])('copies %s', (_case, start, end, expected) => {
    const container = renderDoc();
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it('copies a mention chip as the marker that was typed', () => {
    const content = 'ask @<claude> or see #<ses_1a2b>';
    const container = renderDoc(content, [
      { kind: 'agent', name: 'claude' },
      { kind: 'session', session_id: 'ses_1a2b', title: 'Release plan' },
    ]);
    const whole = document.createRange();
    whole.selectNodeContents(container);

    expect(selectedMarkdown(whole, container)).toBe(content);
    expect(selectedMarkdown(rangeOver(container, ['aude', 'before'], ['or', 'after']), container))
      .toBe('@<claude> or');
    // The chip shows the session's title, which is nowhere in the source.
    expect(selectedMarkdown(rangeOver(container, ['lease', 'before'], ['pla', 'after']), container))
      .toBe('#<ses_1a2b>');
  });

  it('copies a citation badge as the link it stands for', () => {
    // The badge shows its number; the source holds the link the backend wrote.
    const link = '[example.com](https://example.com/source)';
    const content = `Cited ${link} here.`;
    const binding = bindCitations([{
      index: 1,
      ref_id: 'turn0view0',
      title: 'Source',
      url: 'https://example.com/source',
      label: 'example.com',
      spans: [[6, 6 + link.length]],
      body_sha256: bodyDigest(content),
    }], content);
    const { container } = render(<Markdown content={content} citations={binding} />);
    const badge = container.querySelector('a[data-citation-index]')!;
    const range = document.createRange();
    range.selectNodeContents(badge);

    expect(selectedMarkdown(range, container)).toBe(link);
  });

  it.each<[string, string, Edge, Edge, string]>([
    ['a quote', '> alpha\n> beta', ['pha', 'before'], ['be', 'after'], 'pha\nbe'],
    ['a list item', '- alpha\n  beta\n- next', ['pha', 'before'], ['be', 'after'], 'pha\nbe'],
    ['code in a quote', '> ```\n> x = 1\n>   y = 2\n> ```', ['x', 'before'], ['y', 'after'], 'x = 1\n  y'],
    ['code in a list item', '- item\n\n  ```\n  x = 1\n    y = 2\n  ```', ['1', 'before'], ['y', 'after'], '1\n  y'],
  ])('drops the container syntax from a cut inside %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it.each<[string, string, Edge, Edge, string]>([
    ['a quote', '> alpha\n>\n> beta\n\nafter', ['pha', 'before'], ['af', 'after'], '> alpha\n>\n> beta\n\nafter'],
    ['a nested list', '- top\n  - inner\n    - deep\n- next', ['ee', 'before'], ['ne', 'after', 1], '- deep\n- next'],
  ])('keeps the container syntax of blocks copied whole from %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it('copies a footnote with its definition', () => {
    const container = renderDoc('A claim[^1] here.\n\nNext.\n\n[^1]: The source.');
    expect(selectedMarkdown(rangeOver(container, ['claim', 'before'], ['here', 'after']), container))
      .toBe('claim[^1] here\n\n[^1]: The source.');
  });

  it('leaves out a block the selection only touches the edge of', () => {
    // A triple-click selects to the start of the next block.
    const container = renderDoc('first para\n\nsecond para');
    const range = document.createRange();
    range.setStart(...pointAt(container, ['first', 'before']));
    range.setEnd(container.querySelectorAll('p')[1], 0);

    expect(selectedMarkdown(range, container)).toBe('first para');
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

    expect(selectedMarkdown(range, container)).toBe('first **bubble** text\n\n- second');
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
