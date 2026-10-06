import type { TextEdit } from '@/lib/citations';

// Selection → the Markdown that was written.
//
// The transcript shows rendered Markdown, but a reader copying from it wants
// the source the bubble was rendered from: `**b**`, not `b`. The renderer
// therefore marks every element it draws with the source range it came from
// (`data-md-start` / `data-md-end`, UTF-16 offsets into the text ReactMarkdown
// parsed), marks the text blocks among them (paragraphs, headings, list items,
// code blocks, tables, rules), and binds each rendered root to that text.
//
// A selection is first narrowed to the characters it selects. One inside one
// text block — or one table cell — is cut at those characters, found by aligning
// the rendered text before each endpoint against the source from where the
// block's content begins; a character's source includes its backslash escape. Only formatting is faithful enough to cut through: its
// rendering is its source minus the delimiters. So emphasis or a code span is cut
// only while the selection stays inside it, and anything else inline — a link,
// image, mention chip, citation badge or file card, whose rendering is not its
// source text — is copied whole. Container syntax a cut carries on its
// continuation lines (a quote's `>`, a list item's indentation) is not part of
// what was selected and is dropped.
//
// A selection that crosses blocks copies every block it touches, whole and with
// the container syntax on its lines, so a list item keeps its `- ` and a quote
// its `>` — except a list item whose marker line the copy starts after, whose
// indentation is dropped from the lines inside it. A selection that reaches a
// bubble's edge reaches its source's edge, so a whole bubble copies byte for
// byte. A reference-style link or footnote brings its definition along when the
// copy would otherwise lose it.
//
// The promise is fidelity: the copy is the source of what was selected. A
// fragment whose meaning depends on context it no longer has (a `# ` that began
// mid-sentence, an item numbered 2 right after a paragraph) can render
// differently when pasted on its own, as any copied piece of a document can.

const START = 'data-md-start';
const END = 'data-md-end';
const BLOCK = 'data-md-block';
const DEF_START = 'data-md-def-start';
const DEF_END = 'data-md-def-end';
const CONTENT_START = 'data-md-content-start';
const ROOT = 'data-md-root';
const MARKED = `[${START}]`;
const BLOCKS = `[${BLOCK}]`;
// Where an in-block cut is measured: a text block, or one cell of a table.
const LEAVES = `td, th, ${BLOCKS}`;

// What shows content without text.
const TEXTLESS = new Set(['IMG', 'HR']);
const isTextless = (node: Node) => node.nodeType === Node.ELEMENT_NODE && TEXTLESS.has((node as Element).tagName);

const BLOCK_TAGS = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'pre', 'table', 'hr']);
const FORMATTING = new Set(['STRONG', 'EM', 'DEL', 'CODE']);
// The properties these plugins write, and the attributes they render as.
const ATTRIBUTES = [
  ['dataMdStart', START],
  ['dataMdEnd', END],
  ['dataMdBlock', BLOCK],
  ['dataMdDefStart', DEF_START],
  ['dataMdDefEnd', DEF_END],
  ['dataMdContentStart', CONTENT_START],
] as const;

/** What a rendered Markdown root was rendered from. */
type MarkdownSource = {
  /** The text the caller handed the renderer. */
  content: string;
  /** The text ReactMarkdown parsed: `content` after the renderer's own rewrites. */
  text: string;
  /** Each rewrite pass's edits, in the order the passes ran (see `TextEdit`). */
  passes: readonly (readonly TextEdit[])[];
};

const sources = new WeakMap<Element, MarkdownSource>();

/** Bind a rendered root to its source, so a selection inside it can be copied as Markdown. */
export function bindMarkdownSource(root: Element, source: MarkdownSource): void {
  sources.set(root, source);
  root.setAttribute(ROOT, '');
  root.setAttribute(START, '0');
  root.setAttribute(END, String(source.text.length));
}

type HastNode = {
  type?: string;
  tagName?: string;
  position?: { start?: { offset?: number }; end?: { offset?: number } };
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

type MdastNode = {
  type?: string;
  identifier?: unknown;
  position?: { start?: { offset?: number }; end?: { offset?: number } };
  data?: { hProperties?: Record<string, unknown> };
  children?: MdastNode[];
};

function eachNode<T extends { children?: T[] }>(node: T, visit: (node: T) => void): void {
  visit(node);
  node.children?.forEach((child) => eachNode(child, visit));
}

// A fenced block's code is the lines between its fences — what the reader sees
// and selects — so the `code` inside a `pre` is marked with that interior. An
// indented block has no fence lines and keeps its whole range.
function codeInterior(source: string, start: number, end: number): [number, number] {
  const raw = source.slice(start, end);
  const fence = /^[ \t]*(`{3,}|~{3,})/.exec(raw);
  if (!fence) return [start, end];
  const open = raw.indexOf('\n');
  if (open < 0) return [end, end];
  const close = raw.lastIndexOf('\n');
  const closing = new RegExp(`^[ \\t]*${fence[1][0]}{${fence[1].length},}[ \\t]*$`);
  return [start + open + 1, close > open && closing.test(raw.slice(close + 1)) ? start + close + 1 : end];
}

/**
 * Record what the rendered DOM no longer shows: where each list item, heading
 * and table cell's content begins after its syntax, and which definition each
 * reference needs, which renders nowhere near it.
 */
export function remarkSourceAnchors() {
  return (tree: unknown) => {
    eachNode(tree as MdastNode, (node) => {
      if (node.type !== 'listItem' && node.type !== 'heading' && node.type !== 'tableCell') return;
      const start = node.children?.[0]?.position?.start?.offset;
      if (typeof start === 'number') ((node.data ??= {}).hProperties ??= {}).dataMdContentStart = start;
    });
    const kind = (type?: string) => {
      if (type === 'definition' || type === 'linkReference' || type === 'imageReference') return 'link';
      if (type === 'footnoteDefinition' || type === 'footnoteReference') return 'note';
      return null;
    };
    const definitions = new Map<string, [number, number]>();
    eachNode(tree as MdastNode, (node) => {
      const start = node.position?.start?.offset;
      const end = node.position?.end?.offset;
      if (node.type !== 'definition' && node.type !== 'footnoteDefinition') return;
      if (typeof node.identifier !== 'string' || typeof start !== 'number' || typeof end !== 'number') return;
      // The first definition of an identifier is the one a reference resolves to.
      const key = `${kind(node.type)}:${node.identifier}`;
      if (!definitions.has(key)) definitions.set(key, [start, end]);
    });
    eachNode(tree as MdastNode, (node) => {
      if (node.type === 'definition' || node.type === 'footnoteDefinition') return;
      const type = kind(node.type);
      if (!type || typeof node.identifier !== 'string') return;
      const span = definitions.get(`${type}:${node.identifier}`);
      if (!span) return;
      const properties = ((node.data ??= {}).hProperties ??= {});
      properties.dataMdDefStart = span[0];
      properties.dataMdDefEnd = span[1];
    });
  };
}

/** Mark every element with the source range it was rendered from, and each text block as one. */
export function rehypeSourceSpans() {
  return (tree: unknown, file: { value?: unknown }) => {
    const source = typeof file.value === 'string' ? file.value : '';
    const visit = (node: HastNode, parent: HastNode | null) => {
      const start = node.position?.start?.offset;
      const end = node.position?.end?.offset;
      if (node.type === 'element' && typeof start === 'number' && typeof end === 'number') {
        const [from, to] = node.tagName === 'code' && parent?.tagName === 'pre'
          ? codeInterior(source, start, end)
          : [start, end];
        const properties = (node.properties ??= {});
        properties.dataMdStart = from;
        properties.dataMdEnd = to;
        if (BLOCK_TAGS.has(node.tagName ?? '')) properties.dataMdBlock = 'true';
      }
      node.children?.forEach((child) => visit(child, node));
    };
    visit(tree as HastNode, null);
  };
}

/** The marks for the element a custom component renders in place of hast `node`. */
export function sourceSpanProps(node: unknown): Record<string, string | number> {
  const properties = (node as HastNode | undefined)?.properties ?? {};
  const props: Record<string, string | number> = {};
  for (const [property, attribute] of ATTRIBUTES) {
    const value = properties[property];
    if (typeof value === 'string' || typeof value === 'number') props[attribute] = value;
  }
  return props;
}

type Point = { node: Node; offset: number };
type Side = 'start' | 'end';

const spanStart = (el: Element) => Number(el.getAttribute(START));
const spanEnd = (el: Element) => Number(el.getAttribute(END));
const blank = (text: string) => text.trim() === '';
const lineStart = (text: string, offset: number) => text.lastIndexOf('\n', offset - 1) + 1;
const hostOf = (point: Point) =>
  point.node.nodeType === Node.ELEMENT_NODE ? (point.node as Element) : point.node.parentElement;

function textBetween(root: Element, set: (range: Range) => void): string {
  const range = root.ownerDocument.createRange();
  set(range);
  return range.toString();
}

// The innermost element matching `selector` that holds `point`, below `root`.
function enclosing(point: Point, root: Element, selector: string): Element | null {
  const el = hostOf(point)?.closest(selector) ?? null;
  return el && el !== root && root.contains(el) ? el : null;
}

// Marked descendants with no marked element between them and `host`.
function markedChildren(host: Element): Element[] {
  return Array.from(host.querySelectorAll(MARKED)).filter(
    (el) => el.parentElement?.closest(MARKED) === host,
  );
}

// Walk `typed` through `text[from, to)`, skipping the Markdown syntax between
// its characters; the offset just after the last one matched. A character with
// no counterpart (text a component drew rather than the source) is passed over,
// and whitespace anchors nothing: the renderer draws some of its own (after a
// task checkbox), and a narrowed selection's endpoints never rest on it.
function align(text: string, from: number, to: number, typed: string): number {
  let cursor = from;
  for (const ch of typed) {
    if (blank(ch)) continue;
    const at = text.indexOf(ch, cursor);
    if (at >= 0 && at + ch.length <= to) cursor = at + ch.length;
  }
  return cursor;
}

// The source offset of `point` inside `host`, its innermost marked element.
function offsetIn(text: string, host: Element, point: Point, side: Side): number {
  const from = host.hasAttribute(CONTENT_START) ? Number(host.getAttribute(CONTENT_START)) : spanStart(host);
  const to = spanEnd(host);
  const here = host.ownerDocument.createRange();
  here.setStart(point.node, point.offset);
  let before: Element | null = null;
  let after: Element | null = null;
  for (const child of markedChildren(host)) {
    if (here.comparePoint(child, 0) < 0) before = child;
    else {
      after = child;
      break;
    }
  }
  // Align from the nearest marked child behind the point, so syntax inside it
  // (a link's destination, say) can never be mistaken for the text after it.
  const base = before ? spanEnd(before) : from;
  const typed = textBetween(host, (range) => {
    if (before) range.setStartAfter(before);
    else range.setStart(host, 0);
    range.setEnd(point.node, point.offset);
  });
  if (side === 'end') return blank(typed) ? base : align(text, base, to, typed);
  const rest = textBetween(host, (range) => {
    range.setStart(point.node, point.offset);
    if (after) range.setEndBefore(after);
    else range.setEnd(host, host.childNodes.length);
  });
  // The next thing the selection reaches is a marked child: start at its
  // syntax, not at its first character.
  if (blank(rest)) return after ? spanStart(after) : to;
  const cursor = align(text, base, to, typed);
  // Only a code line's indentation can be selected whitespace: it is written
  // right before the line's first character.
  const lead = /^[ \t]*/.exec(rest)![0].length;
  const next = String.fromCodePoint(rest.codePointAt(lead)!);
  const at = text.indexOf(next, cursor);
  if (at < 0 || at >= to) return cursor;
  if (lead) return Math.max(lineStart(text, at), at - lead);
  // Escaped punctuation is written with its backslash.
  return at > cursor && text[at - 1] === '\\' && /[!-/:-@[-`{-~]/.test(next) ? at - 1 : at;
}

// Where a cut inside `leaf` falls for `point`. An inline element that is not
// formatting, or formatting the selection leaves, is taken whole.
function cutOffset(text: string, leaf: Element, point: Point, side: Side, other: Point): number {
  const chain: Element[] = [];
  for (
    let el = hostOf(point)?.closest(MARKED) ?? null;
    el && el !== leaf && leaf.contains(el);
    el = el.parentElement?.closest(MARKED) ?? null
  ) {
    chain.unshift(el);
  }
  const whole = chain.find((el) => !FORMATTING.has(el.tagName) || !el.contains(other.node));
  if (whole) return side === 'start' ? spanStart(whole) : spanEnd(whole);
  return offsetIn(text, chain[chain.length - 1] ?? leaf, point, side);
}

const TAB_STOP = 4;

// What reading a container's syntax off the front of one line leaves: the
// syntax read, the rest of the line, and the column the rest starts at.
type Read = [syntax: string, rest: string, column: number];
type Container = { el: Element; read: (line: string, column: number) => Read };

// Up to `columns` columns of leading whitespace, counted from `column` the way
// CommonMark counts them: a tab advances to the next tab stop, and a tab only
// partly read leaves the rest of its width as spaces.
function readIndent(line: string, columns: number, column: number): Read {
  const limit = column + columns;
  let syntax = '';
  let at = 0;
  while (at < line.length && column < limit) {
    if (line[at] === ' ') {
      syntax += ' ';
      column += 1;
    } else if (line[at] === '\t') {
      const stop = column + TAB_STOP - (column % TAB_STOP);
      if (stop > limit) {
        return [syntax + ' '.repeat(limit - column), ' '.repeat(stop - limit) + line.slice(at + 1), limit];
      }
      syntax += '\t';
      column = stop;
    } else {
      break;
    }
    at += 1;
  }
  return [syntax, line.slice(at), column];
}

// A quote marker: up to three columns of indent, `>`, and one optional column
// after it. A lazy continuation line has none, and is left as it is.
function readQuote(line: string, column: number): Read {
  const [indent, rest, at] = readIndent(line, 3, column);
  if (!rest.startsWith('>')) return ['', line, column];
  const [space, after, end] = readIndent(rest.slice(1), 1, at + 1);
  return [`${indent}>${space}`, after, end];
}

// The column `offset` sits at on its line.
function columnAt(text: string, offset: number): number {
  let column = 0;
  for (let at = lineStart(text, offset); at < offset; at += 1) {
    column = text[at] === '\t' ? column + TAB_STOP - (column % TAB_STOP) : column + 1;
  }
  return column;
}

// How far a list item's continuation lines are indented: to its content, or
// one column past its marker when the content is blank or is indented code. A
// footnote definition's continuation lines are indented four columns.
function itemWidth(text: string, item: Element): number {
  const start = spanStart(item);
  if (text.startsWith('[^', start)) return TAB_STOP;
  const marker = /^(?:[-*+]|\d{1,9}[.)])/.exec(text.slice(start))?.[0] ?? '';
  const column = columnAt(text, start);
  const end = text.indexOf('\n', start);
  const line = text.slice(start + marker.length, end < 0 ? text.length : end);
  const [, rest, content] = readIndent(line, Infinity, column + marker.length);
  const spaces = content - column - marker.length;
  return !rest || spaces > 4 ? marker.length + 1 : content - column;
}

// Indentation up to an absolute column, the way a container's content starts
// at one: a nested item's own marker may sit a few columns into its parent's.
const indentTo = (el: Element, target: number): Container => ({
  el,
  read: (line, column) => readIndent(line, Math.max(0, target - column), column),
});

// A list item's continuation lines are indented to the column its content starts at.
const itemContainer = (text: string, item: Element) =>
  indentTo(item, columnAt(text, spanStart(item)) + itemWidth(text, item));

// The containers around `el` (itself excluded) whose syntax its continuation
// lines repeat, outermost first: a quote's `>`, a list item's indentation.
function containersOf(el: Element, root: Element, text: string): Container[] {
  const containers: Container[] = [];
  for (let parent = el.parentElement; parent && parent !== root; parent = parent.parentElement) {
    if (parent.tagName === 'BLOCKQUOTE') {
      containers.unshift({ el: parent, read: readQuote });
    } else if (parent.tagName === 'LI' && parent.hasAttribute(START)) {
      containers.unshift(itemContainer(text, parent));
    }
  }
  return containers;
}

// A code block's lines also carry the block's own indentation: an indented
// block's four columns, or as many as its opening fence is indented.
function codeContainer(text: string, block: Element, containers: Container[]): Container {
  const start = lineStart(text, spanStart(block));
  const end = text.indexOf('\n', start);
  const line = text.slice(start, end < 0 ? text.length : end);
  let [, rest, column] = ['', line, 0] as Read;
  for (const container of containers) [, rest, column] = container.read(rest, column);
  const [, fence, fenceColumn] = readIndent(rest, Infinity, column);
  return indentTo(block, /^(?:`{3,}|~{3,})/.test(fence) ? fenceColumn : column + TAB_STOP);
}

// `line` with each container's syntax read off its front, kept or dropped.
function rewriteLine(line: string, containers: Container[], keep: (container: Container) => boolean): string {
  let kept = '';
  let rest = line;
  let column = 0;
  for (const container of containers) {
    const [syntax, after, next] = container.read(rest, column);
    if (keep(container)) kept += syntax;
    rest = after;
    column = next;
  }
  return kept + rest;
}

function selectsAllOf(el: Element, start: Point, end: Point): boolean {
  return blank(textBetween(el, (range) => {
    range.setStart(el, 0);
    range.setEnd(start.node, start.offset);
  })) && blank(textBetween(el, (range) => {
    range.setStart(end.node, end.offset);
    range.setEnd(el, el.childNodes.length);
  }));
}

// The blocks a selection crossing blocks copies: the innermost block holding
// each endpoint, when the selection takes any of its text or the image or rule
// the endpoint rests on, and every block between them.
function selectedBlocks(range: Range, root: Element, start: Point, end: Point): Element[] {
  const startBlock = enclosing(start, root, BLOCKS);
  const endBlock = enclosing(end, root, BLOCKS);
  return Array.from(root.querySelectorAll(BLOCKS)).filter((block) => {
    if (block !== startBlock && block !== endBlock) {
      return range.intersectsNode(block) && !block.contains(start.node) && !block.contains(end.node);
    }
    if (isTextless(block === startBlock ? start.node : end.node)) return true;
    return !blank(textBetween(root, (taken) => {
      if (block === startBlock) taken.setStart(start.node, start.offset);
      else taken.setStart(block, 0);
      if (block === endBlock) taken.setEnd(end.node, end.offset);
      else taken.setEnd(block, block.childNodes.length);
    }));
  });
}

// An offset in the parsed text, carried back through the rewrite passes into
// `content`. An offset inside a rewritten span widens to cover the whole of
// what was rewritten: a mention chip copies as the `@<name>` that was typed.
function toContent(source: MarkdownSource, offset: number, side: Side): number {
  let at = offset;
  for (let pass = source.passes.length - 1; pass >= 0; pass -= 1) {
    const edits = [...source.passes[pass]].sort((left, right) => left.start - right.start);
    let shift = 0;
    let mapped: number | null = null;
    for (const edit of edits) {
      const from = edit.start + shift;
      if (at <= from) break;
      if (at < from + edit.inserted) {
        mapped = side === 'start' ? edit.start : edit.end;
        break;
      }
      shift += edit.inserted - (edit.end - edit.start);
    }
    at = mapped ?? at - shift;
  }
  return at;
}

// `text[from, to)` as the caller wrote it, followed by any definition a
// reference in it needs and it does not already hold.
function copyOf(
  source: MarkdownSource,
  root: Element,
  from: number,
  to: number,
  tidy: (markdown: string) => string = (markdown) => markdown,
): string {
  const slice = (start: number, end: number) =>
    source.content.slice(toContent(source, start, 'start'), toContent(source, end, 'end'));
  // A definition brought along can itself hold references (a footnote citing a
  // reference link), so collect until nothing copied needs anything more.
  const copied: Array<[number, number]> = [[from, to]];
  const holds = (start: number, end: number) => copied.some(([left, right]) => start >= left && end <= right);
  const references = Array.from(root.querySelectorAll(`[${DEF_START}]`)).filter((el) => el.hasAttribute(START));
  for (let grew = true; grew;) {
    grew = false;
    for (const el of references) {
      const definition: [number, number] = [Number(el.getAttribute(DEF_START)), Number(el.getAttribute(DEF_END))];
      if (!holds(spanStart(el), spanEnd(el)) || holds(...definition)) continue;
      copied.push(definition);
      grew = true;
    }
  }
  const needed = copied.slice(1).sort(([left], [right]) => left - right);
  return [tidy(slice(from, to)), ...needed.map(([start, end]) => slice(start, end))].join('\n\n');
}

function copyFromRoot(range: Range, root: Element, start: Point, end: Point): string {
  const source = sources.get(root)!;
  const { text } = source;
  const startsInside = root.contains(start.node);
  const endsInside = root.contains(end.node);

  const leaf = startsInside && endsInside ? enclosing(start, root, LEAVES) : null;
  const wholeBlock = leaf?.hasAttribute(BLOCK) && selectsAllOf(leaf, start, end);
  if (leaf && leaf === enclosing(end, root, LEAVES) && !wholeBlock) {
    const from = cutOffset(text, leaf, start, 'start', end);
    const to = cutOffset(text, leaf, end, 'end', start);
    if (to <= from) return '';
    // The leaf's own continuation indentation is container syntax here too.
    const containers = containersOf(leaf, root, text);
    if (leaf.tagName === 'LI') containers.push(itemContainer(text, leaf));
    if (leaf.tagName === 'PRE' || leaf.firstElementChild?.tagName === 'PRE') {
      containers.push(codeContainer(text, leaf, containers));
    }
    const fromLineStart = from === lineStart(text, from);
    return copyOf(source, root, from, to, (markdown) =>
      markdown
        .split('\n')
        .map((line, index) => (index > 0 || fromLineStart ? rewriteLine(line, containers, () => false) : line))
        .join('\n'),
    );
  }

  const before = startsInside && !blank(textBetween(root, (taken) => {
    taken.setStart(root, 0);
    taken.setEnd(start.node, start.offset);
  }));
  const after = endsInside && !blank(textBetween(root, (taken) => {
    taken.setStart(end.node, end.offset);
    taken.setEnd(root, root.childNodes.length);
  }));
  if (!before && !after) return source.content;
  const blocks = selectedBlocks(range, root, start, end);
  if (!blocks.length) return '';
  const first = blocks[0];
  const from = before ? lineStart(text, spanStart(first)) : 0;
  const to = after ? Math.max(...blocks.map(spanEnd)) : text.length;
  // A list item whose marker line the copy starts after is not in the copy: its
  // indentation is dropped from the lines inside it, which then read as they did
  // inside it. Every other container keeps its syntax.
  const containers = before ? containersOf(first, root, text) : [];
  const opened = ({ el }: Container) => el.tagName === 'LI' && spanStart(el) < from;
  if (!containers.some(opened)) return copyOf(source, root, from, to);
  const ends = containers.map(({ el }) => toContent(source, spanEnd(el), 'end'));
  return copyOf(source, root, from, to, (markdown) => {
    let at = toContent(source, from, 'start');
    return markdown
      .split('\n')
      .map((line) => {
        const around = containers.filter((_, index) => at < ends[index]);
        at += line.length + 1;
        return rewriteLine(line, around, (container) => !opened(container));
      })
      .join('\n');
  });
}

// The selection narrowed to what it selects: characters, and images or rules.
// An endpoint resting on an element's edge, between blocks, or in whitespace at
// either end selects nothing there — a triple-click ends at the start of the
// next block — except a code line's indentation, which is part of its code.
function selectedCharacters(range: Range): Range | null {
  const scope = range.commonAncestorContainer;
  const nodes: Node[] = [];
  if (scope.nodeType === Node.TEXT_NODE) {
    nodes.push(scope);
  } else {
    const walker = scope.ownerDocument!.createTreeWalker(scope, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const content = node.nodeType === Node.TEXT_NODE || isTextless(node);
      if (content && range.intersectsNode(node)) nodes.push(node);
    }
  }
  const selected = (node: Text) => {
    const from = node === range.startContainer ? range.startOffset : 0;
    const to = node === range.endContainer ? range.endOffset : node.data.length;
    return { from, text: node.data.slice(from, to) };
  };
  const holds = (node: Node) => node.nodeType !== Node.TEXT_NODE || !blank(selected(node as Text).text);
  const first = nodes.find(holds);
  const last = [...nodes].reverse().find(holds);
  if (!first || !last) return null;
  const narrowed = range.cloneRange();
  if (first.nodeType === Node.TEXT_NODE) {
    const head = selected(first as Text);
    let at = head.from + head.text.search(/\S/);
    if (first.parentElement?.closest('pre')) {
      while (at > head.from && /[ \t]/.test((first as Text).data[at - 1])) at -= 1;
    }
    narrowed.setStart(first, at);
  } else {
    narrowed.setStart(first, 0);
  }
  if (last.nodeType === Node.TEXT_NODE) {
    const tail = selected(last as Text);
    narrowed.setEnd(last, tail.from + tail.text.search(/\s*$/));
  } else {
    narrowed.setEnd(last, 0);
  }
  return narrowed;
}

// The rendered Markdown roots `range` reaches inside `container`, in document order.
function markdownRoots(range: Range, container: Element): Element[] {
  return Array.from(container.querySelectorAll(`[${ROOT}]`)).filter(
    (root) => sources.has(root) && range.intersectsNode(root),
  );
}

/**
 * The Markdown source of what `range` selects inside `container`, or null when
 * it reaches no rendered Markdown. A selection across several bubbles copies
 * each bubble's part, separated by a blank line; text outside any bubble (names,
 * timestamps) is not Markdown and is left out.
 */
export function selectedMarkdown(range: Range, container: Element): string | null {
  const selected = selectedCharacters(range);
  if (!selected) return null;
  const start: Point = { node: selected.startContainer, offset: selected.startOffset };
  const end: Point = { node: selected.endContainer, offset: selected.endOffset };
  const parts = markdownRoots(selected, container).map((root) => copyFromRoot(selected, root, start, end));
  return parts.filter((part) => !blank(part)).join('\n\n') || null;
}

/** `range` widened to cover every bubble it reaches, or null when it reaches none. */
export function wholeMarkdownRange(range: Range, container: Element): Range | null {
  const roots = markdownRoots(range, container);
  if (!roots.length) return null;
  const last = roots[roots.length - 1];
  const whole = container.ownerDocument.createRange();
  whole.setStart(roots[0], 0);
  whole.setEnd(last, last.childNodes.length);
  return whole;
}
