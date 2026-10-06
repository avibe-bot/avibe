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
    // Indentation is counted in columns, a tab reaching the next stop of four.
    ['a tab-indented list item', '-\talpha\n\tbeta', ['pha', 'before'], ['be', 'after'], 'pha\nbe'],
    ['code in a tab-indented list item', '-\titem\n\n\t```\n\tx = 1\n\t\ty = 2\n\t```', ['1', 'before'], ['y', 'after'], '1\n\ty'],
    ['a footnote definition', 'A[^1].\n\n[^1]: first line\n    second line', ['line', 'before'], ['sec', 'after'], 'line\nsec'],
  ])('drops the container syntax from a cut inside %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it.each<[string, string, Edge, Edge, string]>([
    ['a quote', '> alpha\n>\n> beta\n\nafter', ['pha', 'before'], ['af', 'after'], '> alpha\n>\n> beta\n\nafter'],
    ['a nested list', '- top\n  - inner\n    - deep\n- next', ['ee', 'before'], ['ne', 'after', 1], '- deep\n- next'],
    // Only the lines still inside the list item the copy leaves lose its indentation.
    ['a nested list into code', '- top\n  - inner\n- next\n\n```\n  x\n```', ['inn', 'before'], ['x', 'after', 1], '- inner\n- next\n\n```\n  x\n```'],
  ])('keeps the container syntax of blocks copied whole from %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it('copies a tab-indented nested list item as a list item, not as code', () => {
    const container = renderDoc('- top\n\t- inner\n- next');
    const copied = selectedMarkdown(rangeOver(container, ['inn', 'before'], ['ne', 'after', 1]), container)!;
    cleanup();
    const pasted = renderDoc(copied);

    expect(pasted.querySelectorAll('li')).toHaveLength(2);
    expect(pasted.querySelector('pre')).toBeNull();
  });

  it('brings along definitions an appended definition needs', () => {
    const container = renderDoc('A claim[^1] here.\n\n[^1]: See [docs][ref].\n\n[ref]: https://example.com/docs');
    expect(selectedMarkdown(rangeOver(container, ['claim', 'before'], ['here', 'after']), container))
      .toBe('claim[^1] here\n\n[^1]: See [docs][ref].\n\n[ref]: https://example.com/docs');
  });

  // GFM links a bare URL up to the next space; the renderer ends it at CJK text.
  it.each<[string, Edge, Edge, string]>([
    ['a cut inside the URL', ['example', 'before'], ['example', 'after'], 'https://example.com/a'],
    ['a cut from the URL into the text after it', ['example', 'before'], ['这个', 'after'], 'https://example.com/a这个'],
  ])('copies a URL split from CJK text whole on %s', (_case, start, end, expected) => {
    const container = renderDoc('打开 https://example.com/a这个页面');
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  it('copies a footnote with its definition', () => {
    const container = renderDoc('A claim[^1] here.\n\nNext.\n\n[^1]: The source.');
    expect(selectedMarkdown(rangeOver(container, ['claim', 'before'], ['here', 'after']), container))
      .toBe('claim[^1] here\n\n[^1]: The source.');
  });

  // The characters of a block's syntax can be the same as its text's.
  it.each<[string, string, Edge, Edge, string]>([
    ['an ordered item', '1. 2024 revenue\n2. 2025 plan', ['2025', 'before'], ['pl', 'after'], '2025 pl'],
    ['a bullet item', '- --force overwrites\n- other', ['--force', 'before'], ['--force', 'after'], '--force'],
    ['a task item', '- [x] xterm support\n- [ ] other', ['xterm', 'before'], ['xterm', 'after'], 'xterm'],
    ['a heading', '## #123 fix\n\nx', ['#12', 'before'], ['#12', 'after'], '#12'],
    // A character's source includes its escape.
    ['an escaped character', '\\# not a heading here', ['# not', 'before'], ['# not', 'after'], '\\# not'],
    ['escaped characters', '2 \\* 3 \\* 4', ['* 3 *', 'before'], ['* 3 *', 'after'], '\\* 3 \\*'],
  ])('copies only the selected text of %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    expect(selectedMarkdown(rangeOver(container, start, end), container)).toBe(expected);
  });

  // An endpoint resting on an element's edge or in whitespace selects nothing there.
  it.each<[string, string, Edge, (container: Element) => [Node, number], string]>([
    ['the next paragraph', 'Intro with **bold** here.\n\nNext para.', ['here', 'before'], (c) => [c.querySelectorAll('p')[1], 0], 'here.'],
    ['the next list item', '- alpha beta\n- gamma', ['beta', 'before'], (c) => [c.querySelectorAll('li')[1], 0], 'beta'],
    ['a code block\'s copy button', '```\nabc\ndef\n```\n\nafter', ['bc', 'before'], (c) => [c.querySelector('button')!, 0], 'bc\ndef'],
    ['the code block itself', '```\nabc\ndef\n```\n\nafter', ['bc', 'before'], (c) => [c.querySelector('pre')!, 1], 'bc\ndef'],
  ])('ends a selection at its last character before %s', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    const range = document.createRange();
    range.setStart(...pointAt(container, start));
    range.setEnd(...end(container));

    expect(selectedMarkdown(range, container)).toBe(expected);
  });

  // A code line's indentation is code: what of it is selected is copied.
  it.each<[string, string, (container: Element) => [Node, number, number], string]>([
    [
      'part of an indented line in a list',
      '- item\n\n  ```\n  x = 1\n    y = 2\n  ```',
      (c) => { const [node, at] = pointAt(c, ['y', 'before']); return [node, at - 1, at + 1]; },
      ' y',
    ],
    [
      'from a line start in Python',
      '```python\ndef f(x):\n    if x:\n        return 1\n    return 0\n```',
      (c) => {
        const node = c.querySelector('code')!.firstChild!;
        const data = node.textContent!;
        return [node, data.indexOf('    if'), data.indexOf('return 1') + 'return 1'.length];
      },
      '    if x:\n        return 1',
    ],
    [
      'from a blank line',
      '```python\nx = 1\n\n    y = 2\n```',
      (c) => {
        const node = c.querySelector('code')!.firstChild!;
        const data = node.textContent!;
        return [node, data.indexOf('\n\n') + 1, data.indexOf('2') + 1];
      },
      '    y = 2',
    ],
  ])('keeps the indentation of %s', (_case, content, at, expected) => {
    const container = renderDoc(content);
    const [node, from, to] = at(container);
    const range = document.createRange();
    range.setStart(node, from);
    range.setEnd(node, to);

    expect(selectedMarkdown(range, container)).toBe(expected);
  });

  // A list item whose marker line is outside the copy takes its indentation with it.
  it.each<[string, string, Edge, (container: Element) => [Node, number], string]>([
    ['a nested item', '- a\n  - b\n    - c\n- d', ['c', 'before'], (c) => [c.querySelectorAll('li')[3], 0], '- c'],
    [
      'a code block in a nested item',
      '- a\n  - b\n\n    ```bash\n    npm i\n    ```\n- c',
      ['npm', 'before'],
      (c) => [c.querySelectorAll('li')[2], 0],
      '```bash\nnpm i\n```',
    ],
  ])('copies the whole of %s without the indentation of items around it', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    const range = document.createRange();
    range.setStart(...pointAt(container, start));
    range.setEnd(...end(container));

    expect(selectedMarkdown(range, container)).toBe(expected);
  });

  // Nested items are commonly indented four columns or a tab; a nested item's
  // content starts at its own column, not at its parent's plus its marker.
  it.each<[string, string, Edge, Edge, string]>([
    ['a four-space nested item into the next', '- top\n    - inner\n        - deep\n- next', ['deep', 'before'], ['next', 'after'], '  - deep\n- next'],
    ['a tab-nested item into the next', '- top\n\t- inner\n\t\t- deep\n- next', ['deep', 'before'], ['next', 'after'], '  - deep\n- next'],
    ['a paragraph in a four-space nested item', '- top\n    - inner\n\n        more text\n- next', ['more', 'before'], ['next', 'after'], '  more text\n- next'],
  ])('copies %s as list items, not code', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    const copied = selectedMarkdown(rangeOver(container, start, end), container);
    expect(copied).toBe(expected);
    cleanup();
    expect(renderDoc(copied!).querySelector('pre')).toBeNull();
  });

  it('cuts code in a four-space nested item at its own indentation', () => {
    const container = renderDoc('- top\n    - inner\n\n        ```\n        x\n          y\n        z\n        ```');
    expect(selectedMarkdown(rangeOver(container, ['x', 'before'], ['y', 'after']), container)).toBe('x\n  y');
  });

  // An image or a rule shows content without text, and selecting it selects it.
  it.each<[string, string, Edge, (container: Element) => [Node, number], string]>([
    ['an image block', 'Here:\n\n![chart](/api/media/abc123)\n\nNext para.', ['Here', 'before'], (c) => [c.querySelectorAll('p')[2], 0], 'Here:\n\n![chart](/api/media/abc123)'],
    ['an inline image', 'See ![chart](/api/media/abc123) and more', ['See', 'before'], (c) => { const img = c.querySelector('img')!; return [img.parentNode!, Array.from(img.parentNode!.childNodes).indexOf(img) + 1]; }, 'See ![chart](/api/media/abc123)'],
    ['a rule', 'Above\n\n---\n\nBelow', ['Above', 'before'], (c) => [c.querySelectorAll('p')[1], 0], 'Above\n\n---'],
  ])('keeps %s at the edge of a selection', (_case, content, start, end, expected) => {
    const container = renderDoc(content);
    const range = document.createRange();
    range.setStart(...pointAt(container, start));
    range.setEnd(...end(container));

    expect(selectedMarkdown(range, container)).toBe(expected);
  });

  it('finds the lines inside a list item in the text as written, mentions and all', () => {
    const content = '- ask @<claude> and @<codex>\n  - sub @<claude> here @<codex>\n- next\n  - keep nested';
    const container = renderDoc(content, [{ kind: 'agent', name: 'claude' }, { kind: 'agent', name: 'codex' }]);

    expect(selectedMarkdown(rangeOver(container, ['sub', 'before'], ['keep', 'after']), container))
      .toBe('- sub @<claude> here @<codex>\n- next\n  - keep nested');
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
