import type { TextEdit } from '@/lib/citations';

// Selection → the Markdown that was written.
//
// The transcript shows rendered Markdown, but a reader copying from it wants
// the source the bubble was rendered from: `**b**`, not `b`. The renderer
// therefore marks every element it draws with the source range it came from
// (`data-md-start` / `data-md-end`, UTF-16 offsets into the text ReactMarkdown
// parsed) and binds each rendered root to that text. A selection is mapped back
// by finding, for each endpoint, the innermost marked element and aligning the
// rendered text before the endpoint against that element's source.
//
// A cut in the middle of a paragraph, heading, list or quote is still Markdown;
// a cut through the middle of a link, emphasis, code span, code block or table
// is not. So an element of the second kind that the selection only partly
// covers is copied whole — selecting from the middle of a bold phrase onwards
// copies the whole `**phrase**`, not a dangling `phrase**`.

const START = 'data-md-start';
const END = 'data-md-end';
const ROOT = 'data-md-root';
const MARKED = `[${START}]`;

// Elements a selection may begin or end inside of and still leave valid
// Markdown. Everything else is copied whole when the selection crosses its edge.
const CUTTABLE = new Set(['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'UL', 'OL', 'BLOCKQUOTE']);

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

/** Mark every element with the source range it was rendered from. */
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
      }
      node.children?.forEach((child) => visit(child, node));
    };
    visit(tree as HastNode, null);
  };
}

/** The marks for the element a custom component renders in place of hast `node`. */
export function sourceSpanProps(node: unknown): Record<string, number> {
  const properties = (node as HastNode | undefined)?.properties;
  const start = properties?.dataMdStart;
  const end = properties?.dataMdEnd;
  return typeof start === 'number' && typeof end === 'number' ? { [START]: start, [END]: end } : {};
}

type Point = { node: Node; offset: number };
type Side = 'start' | 'end';

const spanStart = (el: Element) => Number(el.getAttribute(START));
const spanEnd = (el: Element) => Number(el.getAttribute(END));
const blank = (text: string) => text.trim() === '';

function textBetween(root: Element, set: (range: Range) => void): string {
  const range = root.ownerDocument.createRange();
  set(range);
  return range.toString();
}

// Marked elements enclosing `point`, innermost first, ending with the root.
function markedChain(point: Point, root: Element): Element[] {
  const chain: Element[] = [];
  const host = point.node.nodeType === Node.ELEMENT_NODE ? (point.node as Element) : point.node.parentElement;
  for (let el = host?.closest(MARKED) ?? null; el; el = el.parentElement?.closest(MARKED) ?? null) {
    chain.push(el);
    if (el === root) break;
  }
  return chain;
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

function sourceOffset(root: Element, text: string, point: Point, side: Side, other: Point | null): number {
  const chain = markedChain(point, root);
  const holdsOther = (el: Element) => other !== null && el.contains(other.node);
  // An element that is not cuttable and that the selection leaves is copied whole.
  for (let index = chain.length - 1; index >= 0; index -= 1) {
    const el = chain[index];
    if (el !== root && !CUTTABLE.has(el.tagName) && !holdsOther(el)) {
      return side === 'start' ? spanStart(el) : spanEnd(el);
    }
  }
  let offset = offsetIn(text, chain[0] ?? root, point, side);
  // A selection that leaves an element from its very edge takes that element's
  // own syntax too: a whole list item keeps its `- `, a whole heading its `##`.
  // The bubble's own edge is its source's edge, whatever the selection holds, so
  // a whole bubble copies whole — reference definitions that render nothing included.
  for (const el of chain) {
    if (el !== root && holdsOther(el)) break;
    const outside = textBetween(root, (range) => {
      if (side === 'start') {
        range.setStart(el, 0);
        range.setEnd(point.node, point.offset);
      } else {
        range.setStart(point.node, point.offset);
        range.setEnd(el, el.childNodes.length);
      }
    });
    if (!blank(outside)) break;
    offset = side === 'start' ? spanStart(el) : spanEnd(el);
  }
  return offset;
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
  const parts = markdownRoots(range, container).map((root) => {
    const source = sources.get(root)!;
    const startsInside = root.contains(start.node);
    const endsInside = root.contains(end.node);
    const from = startsInside
      ? sourceOffset(root, source.text, start, 'start', endsInside ? end : null)
      : 0;
    const to = endsInside
      ? sourceOffset(root, source.text, end, 'end', startsInside ? start : null)
      : source.text.length;
    if (to <= from) return '';
    return source.content.slice(toContent(source, from, 'start'), toContent(source, to, 'end'));
  });
  const markdown = parts.filter((part) => !blank(part)).join('\n\n').replace(/^\n+|\s+$/g, '');
  return markdown || null;
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
