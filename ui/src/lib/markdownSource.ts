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
// A selection inside one text block — or one table cell — is cut at the
// characters selected, found by aligning the rendered text before each endpoint
// against the source. Only formatting is faithful enough to cut through: its
// rendering is its source minus the delimiters. So emphasis or a code span is cut
// only while the selection stays inside it, and anything else inline — a link,
// image, mention chip, citation badge or file card, whose rendering is not its
// source text — is copied whole. Container syntax a cut carries on its
// continuation lines (a quote's `>`, a list item's indentation) is not part of
// what was selected and is dropped.
//
// A selection that crosses blocks copies every block it touches, whole and with
// the container syntax on its lines, so a list item keeps its `- ` and a quote
// its `>`. A selection that reaches a bubble's edge reaches its source's edge,
// so a whole bubble copies byte for byte. A reference-style link or footnote
// brings its definition along when the copy would otherwise lose it.

const START = 'data-md-start';
const END = 'data-md-end';
const BLOCK = 'data-md-block';
const DEF_START = 'data-md-def-start';
const DEF_END = 'data-md-def-end';
const ROOT = 'data-md-root';
const MARKED = `[${START}]`;
const BLOCKS = `[${BLOCK}]`;
// Where an in-block cut is measured: a text block, or one cell of a table.
const LEAVES = `td, th, ${BLOCKS}`;

const BLOCK_TAGS = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'pre', 'table', 'hr']);
const FORMATTING = new Set(['STRONG', 'EM', 'DEL', 'CODE']);
// The properties these plugins write, and the attributes they render as.
const ATTRIBUTES = [
  ['dataMdStart', START],
  ['dataMdEnd', END],
  ['dataMdBlock', BLOCK],
  ['dataMdDefStart', DEF_START],
  ['dataMdDefEnd', DEF_END],
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

/** Point each reference at the definition it needs, which renders nowhere near it. */
export function remarkDefinitionSpans() {
  return (tree: unknown) => {
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
// no counterpart (text a component drew rather than the source) is passed over.
function align(text: string, from: number, to: number, typed: string): number {
  let cursor = from;
  for (const ch of typed) {
    const at = text.indexOf(ch, cursor);
    if (at >= 0 && at + ch.length <= to) cursor = at + ch.length;
  }
  return cursor;
}

// The source offset of `point` inside `host`, its innermost marked element.
function offsetIn(text: string, host: Element, point: Point, side: Side): number {
  const from = spanStart(host);
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
  const next = String.fromCodePoint(rest.codePointAt(0)!);
  const at = text.indexOf(next, cursor);
  return at >= 0 && at < to ? at : cursor;
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

// What each container around `leaf` repeats at the start of its continuation
// lines, outermost first: a quote's `>`, a list item's indentation.
function continuationPrefixes(leaf: Element, root: Element, text: string): RegExp[] {
  const prefixes: RegExp[] = [];
  for (let el: Element | null = leaf; el && el !== root; el = el.parentElement) {
    if (el.tagName === 'BLOCKQUOTE') {
      prefixes.unshift(/^[ \t]{0,3}>[ \t]?/);
    } else if (el.tagName === 'LI' && el.hasAttribute(START)) {
      const marker = /^(?:[-*+]|\d{1,9}[.)])[ \t]*/.exec(text.slice(spanStart(el)))?.[0].length ?? 0;
      prefixes.unshift(new RegExp(`^ {0,${marker}}`));
    }
  }
  return prefixes;
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
// each endpoint, when the selection takes any of its text, and every block
// between them.
function selectedBlocks(range: Range, root: Element, start: Point, end: Point): Element[] {
  const startBlock = enclosing(start, root, BLOCKS);
  const endBlock = enclosing(end, root, BLOCKS);
  return Array.from(root.querySelectorAll(BLOCKS)).filter((block) => {
    if (block !== startBlock && block !== endBlock) {
      return range.intersectsNode(block) && !block.contains(start.node) && !block.contains(end.node);
    }
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
  const definitions = new Map<number, number>();
  for (const el of root.querySelectorAll(`[${DEF_START}]`)) {
    const start = Number(el.getAttribute(DEF_START));
    const end = Number(el.getAttribute(DEF_END));
    const used = el.hasAttribute(START) && spanStart(el) >= from && spanEnd(el) <= to;
    if (used && (start < from || end > to)) definitions.set(start, end);
  }
  const needed = [...definitions].sort(([left], [right]) => left - right);
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
    const prefixes = continuationPrefixes(leaf, root, text);
    const strip = (line: string) => prefixes.reduce((rest, prefix) => rest.replace(prefix, ''), line);
    const fromLineStart = from === lineStart(text, from);
    return copyOf(source, root, from, to, (markdown) =>
      markdown
        .split('\n')
        .map((line, index) => (index > 0 || fromLineStart ? strip(line) : line))
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
  const from = before ? Math.min(...blocks.map((block) => lineStart(text, spanStart(block)))) : 0;
  const to = after ? Math.max(...blocks.map(spanEnd)) : text.length;
  // Lines copied from inside a nested container keep their indentation relative
  // to the first one, not to a document they are no longer in.
  return copyOf(source, root, from, to, (markdown) => {
    const indent = before ? /^ */.exec(markdown)![0].length : 0;
    return indent ? markdown.replace(new RegExp(`^ {0,${indent}}`, 'gm'), '') : markdown;
  });
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
  const start: Point = { node: range.startContainer, offset: range.startOffset };
  const end: Point = { node: range.endContainer, offset: range.endOffset };
  const parts = markdownRoots(range, container).map((root) => copyFromRoot(range, root, start, end));
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
