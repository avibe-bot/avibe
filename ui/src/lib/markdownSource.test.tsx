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

const BOLD = 'Before **bold words** after';
const CJK = '前面 **加粗文字** 后面';
const CODE = '```ts\nconst a = 1;\nconst b = 2;\n```';
const LIST = '- first item\n- second `code` item';
const QUOTE = '> q1\n> q2\n>\n> p2\n\nafter';
const TABLE = '| key | val |\n|---|---|\n| x | yes |\n| z | **w** |';
const REFERENCE = '见[文档][docs]和[接口][api]。\n\n[docs]: https://example.com/docs\n[api]: https://example.com/api';
const NOTE = 'A claim[^1] here.\n\n[^1]: First part.\n\n    Second part.\n\nNext.';

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

  it('copies a whole bubble byte for byte once a selection reaches all it shows', () => {
    const content = 'Only **one** paragraph.\n';
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, ['Only', 'before'], ['paragraph.', 'after']), container))
      .toBe(content);
  });

  // A construct the selection covers keeps its Markdown; one it cuts through
  // drops its own syntax and copies what is selected inside it. Nothing that
  // was not selected is copied.
  it.each<[string, string, Edge, Edge, string]>([
    ['a whole emphasis', BOLD, ['bold words', 'before'], ['bold words', 'after'], '**bold words**'],
    ['the inside of an emphasis', BOLD, ['old wor', 'before'], ['old wor', 'after'], 'old wor'],
    ['text starting inside an emphasis', BOLD, ['words', 'before'], ['after', 'after'], 'words after'],
    ['text ending after a whole emphasis', BOLD, ['Before', 'before'], ['words', 'after'], 'Before **bold words**'],
    ['a whole CJK emphasis', CJK, ['加粗文字', 'before'], ['加粗文字', 'after'], '**加粗文字**'],
    ['the inside of a CJK emphasis', CJK, ['粗文', 'before'], ['粗文', 'after'], '粗文'],
    ['CJK text starting inside an emphasis', CJK, ['文字', 'before'], ['后面', 'after'], '文字 后面'],
    ['an emphasis whole inside a cut one', '**bold *it* text** end', ['it', 'before'], ['text', 'after'], '*it* text'],
    ['a cut emphasis around a whole one', '**bold *it* text** end', ['old', 'before'], ['it', 'after'], 'old *it*'],
    ['a whole inline link', 'See [link](https://example.com/a) here.', ['See', 'before'], ['link', 'after'], 'See [link](https://example.com/a)'],
    ['part of a link label', 'See [link](https://example.com/a) here.', ['in', 'before'], ['in', 'after'], 'in'],
    ['a whole code span', 'Run `npm test` now.', ['npm test', 'before'], ['npm test', 'after'], '`npm test`'],
    ['part of a code span', 'Run `npm test` now.', ['test', 'before'], ['now', 'after'], 'test now'],
    // Text in a cut construct copies as it reads; a covered one keeps what was written.
    ['escapes and entities in cut text', 'Keep \\*this\\* &amp; **that** too.', ['*this', 'before'], ['that', 'after'], '*this* & **that**'],
    ['escapes and entities in a whole paragraph', 'Keep \\*this\\* &amp; that.\n\nNext.', ['Keep', 'before'], ['that.', 'after'], 'Keep \\*this\\* &amp; that.'],
    ['emoji and the whitespace between them', 'Grow 🌱  **green** 🌳 tall', ['🌱', 'before'], ['🌳', 'after'], '🌱  **green** 🌳'],
    ['a soft line break', 'line one\nline **two** here', ['one', 'before'], ['two', 'after'], 'one\nline **two**'],
    ['part of a heading', '## Plan **now**\n\nBody.', ['Plan', 'before'], ['Plan', 'after'], 'Plan'],
    ['a whole heading and part of the paragraph after it', '## Plan **now**\n\nBody.', ['Plan', 'before'], ['Bo', 'after'], '## Plan **now**\n\nBo'],
    ['a line of a code block', CODE, ['const b', 'before'], ['const b', 'after'], 'const b'],
    ['the end of a code block and the paragraph after it', `${CODE}\n\nafter`, ['const b', 'before'], ['after', 'after'], 'const b = 2;\n\nafter'],
    ['a whole code block and the paragraph after it', `${CODE}\n\nafter`, ['const a', 'before'], ['after', 'after'], `${CODE}\n\nafter`],
    ['the end of one list item and the start of the next', LIST, ['item', 'before'], ['second', 'after'], 'item\nsecond'],
    ['a whole list item and part of the next', LIST, ['first', 'before'], ['second', 'after'], '- first item\nsecond'],
    ['the items of a nested list', '- top\n  - inner one\n  - inner two\n- next', ['inner one', 'before'], ['inner two', 'after'], '- inner one\n- inner two'],
    ['a code block in a list item', '- step:\n  ```sh\n  npm i\n  ```\n- next', ['npm', 'before'], ['next', 'after'], 'npm i\n- next'],
    // A source over several lines of a quote carries the quote's `>` on each.
    ['a multi-line paragraph of a quote', QUOTE, ['q1', 'before'], ['q2', 'after'], 'q1\nq2'],
    ['two paragraphs of a quote', QUOTE, ['q2', 'before'], ['p2', 'after'], 'q2\n\np2'],
    ['a whole quote', QUOTE, ['q1', 'before'], ['p2', 'after'], '> q1\n> q2\n>\n> p2'],
    ['cells across table rows', TABLE, ['val', 'before'], ['w', 'after'], 'val\nx\tyes\nz\t**w**'],
    ['a whole table and the paragraph after it', `Intro.\n\n${TABLE}\n\nend`, ['key', 'before'], ['end', 'after'], `${TABLE}\n\nend`],
    // A covered reference link brings its definition, which renders nowhere.
    ['a whole reference link', REFERENCE, ['文档', 'before'], ['文档', 'after'], '[文档][docs]\n\n[docs]: https://example.com/docs'],
    ['two reference links', REFERENCE, ['文档', 'before'], ['接口', 'after'], '[文档][docs]和[接口][api]\n\n[docs]: https://example.com/docs\n[api]: https://example.com/api'],
    ['part of a reference link label', REFERENCE, ['文', 'before'], ['文', 'after'], '文'],
    // A footnote body is visible text of its own, so a reference never brings it.
    ['a footnote reference', NOTE, ['claim', 'before'], ['here', 'after'], 'claim[^1] here'],
    ['part of a footnote body', NOTE, ['First', 'before'], ['part', 'after'], 'First part'],
    ['a paragraph and the footnotes rendered after it', NOTE, ['Next', 'before'], ['First', 'after'], 'Next.\n\nFirst'],
  ])('copies only %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  // A whole chip copies as the marker that was typed; part of one is part of its label.
  it.each<[string, Edge, Edge, string]>([
    ['a whole chip', ['#Release plan', 'before'], ['#Release plan', 'after'], '#<ses_1a2b>'],
    ['text and a whole chip', ['or', 'before'], ['plan', 'after'], 'or see #<ses_1a2b>'],
    ['part of a chip', ['lease', 'before'], ['pla', 'after'], 'lease pla'],
  ])('copies %s', (_case, start, end, expected) => {
    const content = 'ask @<claude> or see #<ses_1a2b>\n\nNext.';
    const container = renderDoc(content, [
      { kind: 'agent', name: 'claude' },
      { kind: 'session', session_id: 'ses_1a2b', title: 'Release plan' },
    ]);
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  // An endpoint resting on an element's edge or in whitespace touches nothing there.
  it.each<[string, string, Edge, (container: Element) => [Node, number], string]>([
    ['the next paragraph', 'Intro with **bold** here.\n\nNext para.', ['here', 'before'], (c) => [c.querySelectorAll('p')[1], 0], 'here.'],
    ['the next list item', '- alpha\n- beta\n\nAfter.', ['alpha', 'before'], (c) => [c.querySelectorAll('li')[1], 0], '- alpha'],
    ['the paragraph after a code block', '```\nabc\n```\n\nafter\n\nend', ['bc', 'before'], (c) => [c.querySelectorAll('p')[0], 0], 'bc'],
  ])('ends a selection before %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    const range = document.createRange();
    range.setStart(...pointAt(container, start));
    range.setEnd(...end(container));

    expect(selectedMarkdown(range, container)).toBe(expected);
  });

  it('starts a selection after the end of the paragraph before it', () => {
    const container = renderDoc('First **one**.\n\nSecond para.');
    const range = document.createRange();
    range.setStart(...pointAt(container, ['.', 'after']));
    range.setEnd(...pointAt(container, ['Second', 'after']));

    expect(selectedMarkdown(range, container)).toBe('Second');
  });

  // An image or a rule shows content without text, and selecting it selects all of it.
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

  it.each<[string, Edge, string]>([
    ['part of each bubble', ['bubble', 'before'], '**bubble** text\n\n- second'],
    ['one bubble byte for byte and part of the next', ['Intro', 'before'], 'Intro.\n\nfirst **bubble** text\n\n- second'],
  ])('copies %s a selection crosses, separated by a blank line', (_case, start, expected) => {
    const { container } = render(
      <>
        <Markdown content={'Intro.\n\nfirst **bubble** text'} />
        <span>12:00</span>
        <Markdown content={'- second\n- bubble\n\nOutro.'} />
      </>,
    );
    const range = rangeOver(container, start, ['second', 'after']);

    expect(selectedMarkdown(range, container)).toBe(expected);
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
