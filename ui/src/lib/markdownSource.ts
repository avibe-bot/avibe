import type { TextEdit } from '@/lib/citations';

// Selection → the Markdown that was written, bounded by the selection.
//
// The transcript shows rendered Markdown, but a reader copying from it wants
// the source the bubble was rendered from: `**b**`, not `b`. A copy never
// reaches past what was selected; where the two conflict, the selection wins
// and formatting is what gets dropped. For `Before **bold words** after`:
//
// - `bold words` covers the emphasis and copies `**bold words**`.
// - `old wor` cuts through it and copies `old wor`; `words after` copies
//   `words after`. A cut construct drops its own syntax and copies what is
//   selected inside it, where whatever it holds whole keeps its own Markdown.
// - A selection of everything in a bubble copies the bubble's source byte for
//   byte. Select all does exactly that.
//
// Text in a cut construct copies as it reads, whitespace included, so an
// escape or an entity there copies as the character it shows. Whitespace is
// content (code indentation especially): a construct is covered only when the
// selection holds all of it. A hard break the selection holds keeps its source.
// A construct whose source would carry its container's syntax onto later lines
// (a quote's `>`, an item's indent) copies as a cut one unless that container
// is selected whole too. A complete reference link brings along its
// definition, which renders nowhere; a footnote's body is visible text and is
// never brought along.
//
// The renderer marks every element with the source range it came from
// (`data-md-start` / `data-md-end`, UTF-16 offsets into the text ReactMarkdown
// parsed), marks each reference link with its definition's range, and binds
// each rendered root to that text.

const START = 'data-md-start';
const END = 'data-md-end';
const DEF_START = 'data-md-def-start';
const DEF_END = 'data-md-def-end';
const ROOT = 'data-md-root';

// What shows content without text.
const TEXTLESS = new Set(['IMG', 'HR']);
const isTextless = (node: Node) => node.nodeType === Node.ELEMENT_NODE && TEXTLESS.has((node as Element).tagName);

// The properties these plugins write, and the attributes they render as.
const ATTRIBUTES = [
  ['dataMdStart', START],
  ['dataMdEnd', END],
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

/** Point each reference link at its definition, which renders nowhere. */
export function remarkDefinitionSpans() {
  return (tree: unknown) => {
    const definitions = new Map<string, [number, number]>();
    eachNode(tree as MdastNode, (node) => {
      const start = node.position?.start?.offset;
      const end = node.position?.end?.offset;
      if (node.type !== 'definition') return;
      if (typeof node.identifier !== 'string' || typeof start !== 'number' || typeof end !== 'number') return;
      // The first definition of an identifier is the one a reference resolves to.
      if (!definitions.has(node.identifier)) definitions.set(node.identifier, [start, end]);
    });
    eachNode(tree as MdastNode, (node) => {
      if (node.type !== 'linkReference' && node.type !== 'imageReference') return;
      const span = typeof node.identifier === 'string' ? definitions.get(node.identifier) : undefined;
      if (!span) return;
      const properties = ((node.data ??= {}).hProperties ??= {});
      properties.dataMdDefStart = span[0];
      properties.dataMdDefEnd = span[1];
    });
  };
}

/** Mark every element with the source range it was rendered from. */
export function rehypeSourceSpans() {
  return (tree: unknown) => {
    eachNode(tree as HastNode, (node) => {
      const start = node.position?.start?.offset;
      const end = node.position?.end?.offset;
      if (node.type !== 'element' || typeof start !== 'number' || typeof end !== 'number') return;
      const properties = (node.properties ??= {});
      properties.dataMdStart = start;
      properties.dataMdEnd = end;
    });
  };
}

/** The marks for the element a custom component renders in place of hast `node`. */
export function sourceSpanProps(node: unknown): Record<string, number> {
  const properties = (node as HastNode | undefined)?.properties ?? {};
  const props: Record<string, number> = {};
  for (const [property, attribute] of ATTRIBUTES) {
    const value = properties[property];
    if (typeof value === 'number') props[attribute] = value;
  }
  return props;
}

const spanStart = (el: Element) => Number(el.getAttribute(START));
const spanEnd = (el: Element) => Number(el.getAttribute(END));

// Elements laid out as blocks. A copy separates the parts of two blocks itself;
// the blank text the renderer puts between them is only its line layout.
const BLOCKS = new Set([
  'P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'LI', 'BLOCKQUOTE', 'PRE', 'DIV', 'HR',
  'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'SECTION',
]);
// A table's parts are not Markdown without the table: a cell's range starts at its pipe.
const TABLE_PARTS = new Set(['THEAD', 'TBODY', 'TR', 'TH', 'TD']);
const CELLS = new Set(['TH', 'TD']);
// Containers that repeat their syntax on every line they hold: a quote's `>`, an item's indent.
const LINE_PREFIXED = new Set(['BLOCKQUOTE', 'LI']);

const isBlock = (node: Node | null) => node?.nodeType === Node.ELEMENT_NODE && BLOCKS.has((node as Element).tagName);
const isMarkedBreak = (node: Node | null) => node?.nodeType === Node.ELEMENT_NODE
  && (node as Element).tagName === 'BR' && (node as Element).hasAttribute(START);

// How much of a text node is content. Every character is, whitespace included,
// except what the renderer adds for layout: blank text between blocks, the
// newline after a hard break (whose own source holds it), and the newline that
// ends a code block.
function contentLength(text: Text): number {
  if (text.data.trim() === '' && isBlock(text.parentNode)
    && (!text.previousSibling || isBlock(text.previousSibling))
    && (!text.nextSibling || isBlock(text.nextSibling))) return 0;
  if (text.data === '\n' && isMarkedBreak(text.previousSibling)) return 0;
  const code = text.parentElement;
  const endsBlockCode = code?.tagName === 'CODE' && code.parentElement?.tagName === 'PRE'
    && !text.nextSibling && text.data.endsWith('\n');
  return text.data.length - (endsBlockCode ? 1 : 0);
}

type Point = [Node, number];
const before = (node: Node): Point => [node.parentNode!, Array.prototype.indexOf.call(node.parentNode!.childNodes, node)];
const after = (node: Node): Point => [node.parentNode!, before(node)[1] + 1];

// Where what `el` shows begins and ends: its first and last character of
// content, image or rule. A hard break shows a line break, and is all of itself.
function contentBounds(el: Element): [Point, Point] | null {
  if (isTextless(el) || isMarkedBreak(el)) return [before(el), after(el)];
  let first: Point | null = null;
  let last: Point | null = null;
  const walker = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType === Node.TEXT_NODE) {
      const length = contentLength(node as Text);
      if (!length) continue;
      first ??= [node, 0];
      last = [node, length];
    } else if (isTextless(node)) {
      first ??= before(node);
      last = after(node);
    }
  }
  return first && last ? [first, last] : null;
}

// Whether `range` selects everything `el` shows.
function covers(range: Range, el: Element): boolean {
  const bounds = contentBounds(el);
  return !!bounds && bounds.every(([node, offset]) => range.comparePoint(node, offset) === 0);
}

// The selection narrowed to what it selects: characters, images and rules. An
// endpoint resting on an element's edge, between blocks, or in whitespace at
// either end touches nothing there — a triple-click ends at the start of the
// next block, which it did not select.
function selectedContent(range: Range): Range | null {
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
  const holds = (node: Node) => node.nodeType !== Node.TEXT_NODE || selected(node as Text).text.trim() !== '';
  const first = nodes.find(holds);
  const last = [...nodes].reverse().find(holds);
  if (!first || !last) return null;
  const narrowed = range.cloneRange();
  if (first.nodeType === Node.TEXT_NODE) {
    const head = selected(first as Text);
    narrowed.setStart(first, head.from + head.text.search(/\S/));
  } else {
    narrowed.setStartBefore(first);
  }
  if (last.nodeType === Node.TEXT_NODE) {
    const tail = selected(last as Text);
    narrowed.setEnd(last, tail.from + tail.text.search(/\s*$/));
  } else {
    narrowed.setEndAfter(last);
  }
  return narrowed;
}

// An offset in the parsed text, carried back through the rewrite passes into
// `content`. An offset inside a rewritten span widens to cover the whole of
// what was rewritten: a mention chip copies as the `@<name>` that was typed.
function toContent(source: MarkdownSource, offset: number, side: 'start' | 'end'): number {
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

function copyFromRoot(range: Range, root: Element): string {
  const source = sources.get(root)!;
  if (covers(range, root)) return source.content;
  const slice = (start: number, end: number) =>
    source.content.slice(toContent(source, start, 'start'), toContent(source, end, 'end'));

  // What goes between the parts of two blocks: a tab between cells of a row, a
  // line break between rows, otherwise the line break or blank line written
  // between the blocks.
  const separator = (from: Element, to: Element) => {
    if (CELLS.has(from.tagName) && CELLS.has(to.tagName)) return from.parentElement === to.parentElement ? '\t' : '\n';
    if (from.contains(to) || to.contains(from)) return '\n';
    const between = spanEnd(from) <= spanStart(to) ? source.text.slice(spanEnd(from), spanStart(to)) : '';
    return between.split('\n').length === 2 ? '\n' : '\n\n';
  };

  let copied = '';
  let block: Element | null = null;
  const kept: Element[] = [];
  const write = (text: string, within: Element) => {
    if (!text) return;
    if (block && block !== within) copied = copied.replace(/\n+$/, '') + separator(block, within);
    copied += text;
    block = within;
  };
  // A construct the selection covers copies its source; one it cuts through
  // copies what it selects inside. `within` is the block around `node`, and
  // `prefixed` says a container around it repeats its syntax on every line, so
  // a source that runs over several lines would carry that syntax along.
  const visit = (node: Node, within: Element, prefixed: boolean) => {
    if (!range.intersectsNode(node)) return;
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node as Text;
      // Text no construct holds is the renderer's own, like a footnote section's label.
      if (within === root) return;
      const from = text === range.startContainer ? range.startOffset : 0;
      const to = text === range.endContainer ? range.endOffset : text.data.length;
      write(text.data.slice(from, Math.min(to, contentLength(text))), within);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as Element;
    const marked = el.hasAttribute(START);
    const own = marked && BLOCKS.has(el.tagName) ? el : within;
    if (marked && !TABLE_PARTS.has(el.tagName) && covers(range, el)) {
      const markdown = slice(spanStart(el), spanEnd(el));
      if (!prefixed || !markdown.includes('\n')) {
        write(markdown, own);
        kept.push(el);
        return;
      }
    }
    const inner = prefixed || (marked && LINE_PREFIXED.has(el.tagName));
    el.childNodes.forEach((child) => visit(child, own, inner));
  };
  root.childNodes.forEach((child) => visit(child, root, false));

  // A kept reference link brings along its definition, unless that was kept too.
  const holds = (start: number, end: number) => kept.some((el) => start >= spanStart(el) && end <= spanEnd(el));
  const definitions = new Map<number, number>();
  for (const el of kept) {
    for (const reference of [el, ...Array.from(el.querySelectorAll(`[${DEF_START}]`))]) {
      if (!reference.hasAttribute(DEF_START)) continue;
      const start = Number(reference.getAttribute(DEF_START));
      const end = Number(reference.getAttribute(DEF_END));
      if (!holds(start, end)) definitions.set(start, end);
    }
  }
  if (!definitions.size) return copied;
  const appended = [...definitions].sort(([left], [right]) => left - right).map(([start, end]) => slice(start, end));
  return `${copied}\n\n${appended.join('\n')}`;
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
  // The narrowed selection decides which bubbles are reached at all; within
  // them, every character the selection holds is copied, whitespace included.
  const selected = selectedContent(range);
  if (!selected) return null;
  const parts = markdownRoots(selected, container).map((root) => copyFromRoot(range, root));
  return parts.filter((part) => part.trim() !== '').join('\n\n') || null;
}

/** `range` widened to cover every bubble it selects content of, or null when it selects none. */
export function wholeMarkdownRange(range: Range, container: Element): Range | null {
  const selected = selectedContent(range);
  const roots = selected ? markdownRoots(selected, container) : [];
  if (!roots.length) return null;
  const last = roots[roots.length - 1];
  const whole = container.ownerDocument.createRange();
  whole.setStart(roots[0], 0);
  whole.setEnd(last, last.childNodes.length);
  return whole;
}
