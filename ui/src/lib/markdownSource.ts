import type { TextEdit } from '@/lib/citations';

// Selection → the Markdown that was written.
//
// The transcript shows rendered Markdown, but a reader copying from it wants
// the source the bubble was rendered from: `**b**`, not `b`. Copying works in
// whole blocks, because a whole block is self-contained Markdown and a cut
// through one is not (half a list item, half a table, half a link):
//
// - A selection that touches every block of a bubble copies the bubble's
//   source byte for byte. Select all does exactly that.
// - Otherwise it copies the source of each top-level block it touches — a
//   paragraph, heading, list, quote, code block, table, rule or footnote — whole.
//   A selection inside one paragraph copies that paragraph.
// - A reference-style link or footnote in the copy brings its definition along
//   when the definition is not in the copy itself.
//
// The renderer marks every element with the source range it came from
// (`data-md-start` / `data-md-end`, UTF-16 offsets into the text ReactMarkdown
// parsed), marks each reference with its definition's range, and binds each
// rendered root to that text.

const START = 'data-md-start';
const END = 'data-md-end';
const DEF_START = 'data-md-def-start';
const DEF_END = 'data-md-def-end';
const ROOT = 'data-md-root';
const MARKED = `[${START}]`;

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

// A root's top-level blocks: its outermost marked elements. Footnote
// definitions render inside an unmarked section, and are blocks of their own.
function blocksOf(root: Element): Element[] {
  return Array.from(root.querySelectorAll(MARKED)).filter(
    (el) => el.parentElement?.closest(MARKED) === root,
  );
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
  const blocks = blocksOf(root);
  const touched = new Set(blocks.filter((block) => range.intersectsNode(block)));
  if (!touched.size) return '';
  if (touched.size === blocks.length) return source.content;

  // Touched blocks next to each other in the source are copied as one stretch,
  // with what was written between them; a block left out splits the stretch.
  const ordered = [...blocks].sort((left, right) => spanStart(left) - spanStart(right));
  const stretches: Array<[number, number]> = [];
  let open = false;
  for (const block of ordered) {
    if (!touched.has(block)) {
      open = false;
    } else if (open) {
      stretches[stretches.length - 1][1] = spanEnd(block);
    } else {
      stretches.push([spanStart(block), spanEnd(block)]);
      open = true;
    }
  }

  // A definition brought along can itself hold references (a footnote citing a
  // reference link), so collect until nothing copied needs anything more.
  const copied = [...stretches];
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
  const definitions = copied.slice(stretches.length).sort(([left], [right]) => left - right);
  return [...stretches, ...definitions]
    .map(([start, end]) => source.content.slice(toContent(source, start, 'start'), toContent(source, end, 'end')))
    .join('\n\n');
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
  const selected = selectedContent(range);
  if (!selected) return null;
  const parts = markdownRoots(selected, container).map((root) => copyFromRoot(selected, root));
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
