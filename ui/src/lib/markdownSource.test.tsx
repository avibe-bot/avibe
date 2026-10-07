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

const PARAGRAPH = 'Intro with **bold text** and a [link](https://example.com/a) here.';
const LIST = '- first item\n- second `code` item';
const CODE = '```ts\nconst a = 1;\nconst b = 2;\n```';
const TABLE = '| key | val |\n|---|---|\n| x | yes |';

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

  it('copies the whole bubble once a selection touches every block of it', () => {
    const content = 'Only **one** paragraph.\n';
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, ['one', 'before'], ['one', 'after']), container)).toBe(content);
  });

  // Each block a selection touches is copied whole, however little of it is selected.
  it.each<[string, Edge, Edge, string]>([
    ['a few words of a paragraph', ['bold', 'before'], ['and', 'after'], PARAGRAPH],
    ['two characters of a link', ['in', 'before'], ['in', 'after'], PARAGRAPH],
    ['a heading', ['Plan', 'before'], ['Plan', 'after'], '## Plan **now**'],
    ['one list item', ['second', 'before'], ['second', 'after'], LIST],
    ['a line of a code block', ['const b', 'before'], ['const b', 'after'], CODE],
    ['one table cell', ['yes', 'before'], ['yes', 'after'], TABLE],
    ['a paragraph and the list after it', ['here', 'before'], ['first', 'after'], `${PARAGRAPH}\n\n${LIST}`],
    ['a code block and the table after it', ['const a', 'before'], ['key', 'after'], `${CODE}\n\n${TABLE}`],
  ])('copies the blocks under %s', (_case, start, end, expected) => {
    const container = renderDoc();
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it('brings along the definition of a reference in the copy', () => {
    const container = renderDoc();
    expect(selectedMarkdown(rangeOver(container, ['重点', 'before'], ['内容', 'after']), container))
      .toBe('这是**重点**内容。见[文档][docs]。\n\n[docs]: https://example.com/docs');
  });

  it('brings along definitions an appended definition needs', () => {
    const container = renderDoc('A claim[^1] here.\n\nNext.\n\n[^1]: See [docs][ref].\n\n[ref]: https://example.com/docs');
    expect(selectedMarkdown(rangeOver(container, ['claim', 'before'], ['here', 'after']), container))
      .toBe('A claim[^1] here.\n\n[^1]: See [docs][ref].\n\n[ref]: https://example.com/docs');
  });

  it('copies a footnote definition selected in the rendered footnotes once', () => {
    // A two-paragraph footnote is one block, wherever it is written.
    const content = 'A claim[^1] here.\n\n[^1]: First part.\n\n    Second part.\n\nNext.';
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, ['Next', 'before'], ['First', 'after']), container))
      .toBe('[^1]: First part.\n\n    Second part.\n\nNext.');
  });

  it('leaves out a block written between two copied ones but not selected', () => {
    // The footnote is written mid-document but rendered after "Third".
    const container = renderDoc('First.\n\n[^1]: Note.\n\nSecond[^1].\n\nThird.');
    expect(selectedMarkdown(rangeOver(container, ['Third', 'before'], ['Note', 'after']), container))
      .toBe('[^1]: Note.\n\nThird.');
  });

  it('copies a mention chip as the marker that was typed', () => {
    const content = 'ask @<claude> or see #<ses_1a2b>\n\nNext.';
    const container = renderDoc(content, [
      { kind: 'agent', name: 'claude' },
      { kind: 'session', session_id: 'ses_1a2b', title: 'Release plan' },
    ]);
    expect(selectedMarkdown(rangeOver(container, ['lease', 'before'], ['pla', 'after']), container))
      .toBe('ask @<claude> or see #<ses_1a2b>');
  });

  // An endpoint resting on an element's edge or in whitespace touches nothing there.
  it.each<[string, string, Edge, (container: Element) => [Node, number], string]>([
    ['the next paragraph', 'Intro with **bold** here.\n\nNext para.', ['here', 'before'], (c) => [c.querySelectorAll('p')[1], 0], 'Intro with **bold** here.'],
    ['the next list item', '- alpha\n- beta\n\nAfter.', ['alpha', 'before'], (c) => [c.querySelectorAll('li')[1], 0], '- alpha\n- beta'],
    ['the paragraph after a code block', '```\nabc\n```\n\nafter\n\nend', ['bc', 'before'], (c) => [c.querySelectorAll('p')[0], 0], '```\nabc\n```'],
  ])('ends a selection before %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    const range = document.createRange();
    range.setStart(...pointAt(container, start));
    range.setEnd(...end(container));

    expect(selectedMarkdown(range, container)).toBe(expected);
  });

  // An image or a rule shows content without text, and selecting it touches its block.
  it.each<[string, string, Edge, (container: Element) => [Node, number], string]>([
    ['an image block', 'Here:\n\n![chart](/api/media/abc123)\n\nNext para.', ['Here', 'before'], (c) => [c.querySelectorAll('p')[2], 0], 'Here:\n\n![chart](/api/media/abc123)'],
    ['a rule', 'Above\n\n---\n\nBelow', ['Above', 'before'], (c) => [c.querySelectorAll('p')[1], 0], 'Above\n\n---'],
  ])('keeps %s at the edge of a selection', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    const range = document.createRange();
    range.setStart(...pointAt(container, start));
    range.setEnd(...end(container));

    expect(selectedMarkdown(range, container)).toBe(expected);
  });

  it('copies each bubble a selection crosses, separated by a blank line', () => {
    const { container } = render(
      <>
        <Markdown content={'Intro.\n\nfirst **bubble** text'} />
        <span>12:00</span>
        <Markdown content={'- second\n- bubble\n\nOutro.'} />
      </>,
    );
    const range = rangeOver(container, ['bubble', 'before'], ['second', 'after']);

    expect(selectedMarkdown(range, container)).toBe('first **bubble** text\n\n- second\n- bubble');
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
        <Markdown content={'Intro.\n\nfirst **bubble** text'} />
        <span>12:00</span>
        <Markdown content={'- second\n- bubble\n\nOutro.'} />
        <Markdown content="untouched" />
      </>,
    );
    const whole = wholeMarkdownRange(rangeOver(container, ['bubble', 'before'], ['second', 'after']), container)!;

    expect(selectedMarkdown(whole, container))
      .toBe('Intro.\n\nfirst **bubble** text\n\n- second\n- bubble\n\nOutro.');
  });
  it('does not widen into a bubble the selection only ends at the start of', () => {
    const { container } = render(
      <>
        <Markdown content="first bubble" />
        <Markdown content="next bubble" />
      </>,
    );
    const range = document.createRange();
    range.setStart(...pointAt(container, ['first', 'before']));
    range.setEnd(...pointAt(container, ['next', 'before']));

    expect(wholeMarkdownRange(range, container)!.toString()).toBe('first bubble');
  });
});
