// Shared contract for @-mentions (Bots and Agents) and #-session references in
// the chat composer and Agent replies.
//
// Marker convention (the agent-facing, transport-stable form) lives inline in the
// message text and carries the kind (docs/plans/bots.md D3): `@<bot:handle>`
// points at a Bot's permanent conversation, `@<agent:name>` at an Agent (a fresh
// Session each run), and `#<session-id>` at a Session. An untyped `@<name>` in
// older messages means an Agent. A structured sidecar in
// `message.content.references` carries the resolved data (ids, display names,
// session titles) for faithful chip rendering and backend context — see docs/plans.
//
// Humans never read the raw markers (the bubble renders chips), so the format is
// optimized for LLM legibility + robust parsing. The `<…>` form is excised from
// the text BEFORE react-markdown sees it (we rewrite markers to mention links),
// so it never collides with markdown's `<tag>` HTML handling.

// Rewriting a marker moves the text after it, so this pass reports what it
// changed in the form the citation binding carries its measurements across.
import type { EditedText, TextEdit } from './citations';

export type AgentReference = {
  kind: 'agent';
  /** Display name — the `vibe agent run --agent <name>` handle and the chip label. */
  name: string;
  agent_id?: string;
  backend?: string;
};

export type SessionReference = {
  kind: 'session';
  /** Stable `vibe agent run --session-id <id>` handle; carried in the marker. */
  session_id: string;
  /** Current title (or send-time snapshot for legacy rows) — the chip label. */
  title?: string | null;
  /** Whether the current reader can open `/chat/<session_id>`. Set by read-time
   *  server enrichment; omitted on legacy/composer-only sidecars. */
  openable?: boolean;
};

export type BotReference = {
  kind: 'bot';
  /** The frozen handle — the marker and `vibe agent run --bot <handle>` target. */
  handle: string;
  /** Snapshot of the display name at send time — the chip's fallback label. */
  display_name?: string | null;
};

export type MentionReference = AgentReference | BotReference | SessionReference;

/** What a `@` marker points at. */
export type MentionTarget = 'agent' | 'bot' | 'session';

/** Split the inside of an `@<…>` marker into its kind and name. Untyped markers
 *  from before typed mentions mean an Agent. */
export function parseAtMarker(inner: string): { target: 'agent' | 'bot'; name: string } {
  if (inner.startsWith('bot:')) return { target: 'bot', name: inner.slice(4) };
  if (inner.startsWith('agent:')) return { target: 'agent', name: inner.slice(6) };
  return { target: 'agent', name: inner };
}

export const MENTION_TRIGGERS = ['@', '#'] as const;
export type MentionTrigger = (typeof MENTION_TRIGGERS)[number];

// Matches `@<…>` / `#<…>`. Inner text excludes `>` and newlines: agent names with
// `>` are disallowed (enforced at insert time) and session ids are token-safe.
export const MENTION_MARKER_RE = /([@#])<([^>\n]+)>/g;

// Matches bracketed markers OR bare `#ses...` in Agent replies. Lookbehind keeps
// URL fragments (`/#ses...`, `x#ses...`), doubled hashes, and backslash escapes
// untouched while matching inside CJK prose and parentheses (`（#ses...）`).
const AGENT_MENTION_RE =
  /([@#])<([^>\n]+)>|(?<![A-Za-z0-9_/:.?=&@#\\])#(ses(?:_[a-z][a-z0-9-]{1,23}|[A-Za-z0-9][A-Za-z0-9_-]{0,62}))(?![A-Za-z0-9_-])/g;

/** The custom link scheme markers are rewritten to before markdown rendering. */
export const MENTION_LINK_SCHEME = 'avibe-mention';

/** The inline text marker for a reference. */
export function referenceToMarker(ref: MentionReference): string {
  if (ref.kind === 'bot') return `@<bot:${ref.handle}>`;
  if (ref.kind === 'agent') return `@<agent:${ref.name}>`;
  return `#<${ref.session_id}>`;
}

/** Dedupe references by (kind, id) so a marker repeated in the text yields one entry. */
export function dedupeReferences(refs: MentionReference[]): MentionReference[] {
  const seen = new Set<string>();
  const out: MentionReference[] = [];
  for (const ref of refs) {
    const key = ref.kind === 'agent'
      ? `agent:${ref.name}`
      : ref.kind === 'bot'
        ? `bot:${ref.handle}`
        : `session:${ref.session_id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(ref);
  }
  return out;
}

function escapeMarkdownLabel(value: string): string {
  return value.replace(/[\\[\]<>`]/g, (m) => `\\${m}`).replace(/\s*\n\s*/g, ' ');
}

export type LinkifyMentionsOptions = {
  /** Agent-reply mode: only resolved Sessions (in `references` with
   *  `openable !== false`) and resolved Bots (in `references`) become chips;
   *  bare `#ses...` is matched when resolved; unresolved `<…>` markers stay
   *  exact plain text (backslash-escaped before `<` so CommonMark does not
   *  drop `<ses...>` as raw inline HTML). */
  agentReply?: boolean;
};

const INDENTED_CODE_LINE_RE = /^(?: {4}|\t)/;
const LIST_ITEM_LINE_RE = /^ {0,3}(?:[-*+]|\d+[.)])[ \t]/;
const LIST_MARKER_RE = /^(?:[-*+]|\d+[.)])[ \t]/;
const INLINE_CODE_SPAN_RE = /(?<!`)(`+)(?!`)(?:(?!\r?\n\r?\n)[\s\S])*?(?<!`)\1(?!`)/g;

// Segments skipped during mention rewriting:
// 1. Line-start fenced code blocks (any leading indentation so code blocks
//    inside numbered/nested list items stay intact; empty, CRLF, multi-length,
//    and still-open streaming fences all handled).
// 2. Closed mid-line tilde blocks (without crossing blank lines).
// 3. Inline code spans (exact backtick run length, including multiline spans
//    within a paragraph without crossing blank lines).
// 4. Autolinks (`<https://...>`).
// 5. Full Markdown inline links (`[label](dest)` — so mentions inside an
//    existing link label never create nested links; in agentReply mode,
//    bracketed `<…>` markers inside the label are still backslash-escaped so
//    CommonMark does not swallow them as raw HTML).
// 6. Bare link destinations (`](dest)` fallback when a label contains nested
//    brackets).
const SKIP_SEGMENT_RE =
  /(?<lineFence>(?:^|\n)[ \t]*(`{3,})[^`\n]*(?:\n(?:(?:[\s\S]*?\n)??[ \t]*\2`*[ \t\r]*(?=\n|$)|[\s\S]*$)|$)|(?:^|\n)[ \t]*(~{3,})[^\n]*(?:\n(?:(?:[\s\S]*?\n)??[ \t]*\3~*[ \t\r]*(?=\n|$)|[\s\S]*$)|$))|(~{3,})(?:(?!\r?\n\r?\n)[\s\S])*?\4|(?<!`)(`+)(?!`)(?:(?!\r?\n\r?\n)[\s\S])*?(?<!`)\5(?!`)|<https?:\/\/[^>\s]+>|(?<linkLabel>\[[^\]\n]*\])(?<linkDest>\((?:[^()\s]|(?:\([^()\s]*\)))*\))|\]\((?:[^()\s]|(?:\([^()\s]*\)))*\)/g;

/**
 * Rewrite `@<…>` / `#<…>` (and, in Agent replies, resolved bare `#ses...`)
 * markers in `text` into markdown links carrying the `avibe-mention:` scheme,
 * so the shared `Markdown` renderer can show them as chips via its `a`
 * component map. Session labels use the title from `references` when available,
 * else the raw id.
 *
 * A marker becomes a link of a different length, which moves everything after
 * it — including any source citation the backend measured in this same text.
 * So each rewrite is reported as a `TextEdit` in the coordinates of `text`,
 * letting the caller carry those measurements across this pass (see
 * lib/citations). The rewritten text itself is unchanged by that bookkeeping.
 */
export function linkifyMentions(
  text: string,
  references?: MentionReference[],
  options?: LinkifyMentionsOptions,
): EditedText {
  const agentReply = Boolean(options?.agentReply);
  const sessionRefs = new Map<string, SessionReference>();
  const botNames = new Map<string, string>();
  const botRefHandles = new Set<string>();
  const agentRefNames = new Set<string>();
  for (const ref of references ?? []) {
    if (ref.kind === 'session') {
      sessionRefs.set(ref.session_id, ref);
    } else if (ref.kind === 'bot') {
      botRefHandles.add(ref.handle);
      if (ref.display_name) botNames.set(ref.handle, ref.display_name);
    } else if (ref.kind === 'agent') {
      agentRefNames.add(ref.name);
    }
  }
  const edits: TextEdit[] = [];

  const rewriteLegacy = (segment: string, base: number): string =>
    segment.replace(MENTION_MARKER_RE, (full: string, trigger: string, inner: string, at: number) => {
      let kind: MentionTarget = 'session';
      let value = inner;
      let label: string;
      if (trigger === '@') {
        const marker = parseAtMarker(inner);
        kind = marker.target;
        value = marker.name;
        // A Bot's chip shows its display name (the marker keeps the handle, so a
        // rename never alters old messages); the renderer refreshes it from the
        // live Bot when there is one.
        label = escapeMarkdownLabel(`@${kind === 'bot' ? botNames.get(value) || value : value}`);
      } else {
        const sessionRef = sessionRefs.get(inner);
        label = escapeMarkdownLabel(`#${sessionRef?.title || inner}`);
      }
      const link = `[${label}](${MENTION_LINK_SCHEME}:${kind}:${encodeURIComponent(value)})`;
      edits.push({ start: base + at, end: base + at + full.length, inserted: link.length });
      return link;
    });

  const rewriteAgent = (segment: string, base: number): string =>
    segment.replace(
      AGENT_MENTION_RE,
      (full: string, trigger: string | undefined, inner: string | undefined, bareSession: string | undefined, at: number) => {
        if (bareSession) {
          const ref = sessionRefs.get(bareSession);
          if (!ref || ref.openable === false) return full;
          const label = escapeMarkdownLabel(`#${ref.title || bareSession}`);
          const link = `[${label}](${MENTION_LINK_SCHEME}:session:${encodeURIComponent(bareSession)})`;
          edits.push({ start: base + at, end: base + at + full.length, inserted: link.length });
          return link;
        }
        if (!trigger || inner === undefined) return full;
        if (trigger === '#') {
          const sid = inner.trim();
          const ref = sessionRefs.get(sid);
          if (ref && ref.openable !== false) {
            const label = escapeMarkdownLabel(`#${ref.title || sid}`);
            const link = `[${label}](${MENTION_LINK_SCHEME}:session:${encodeURIComponent(sid)})`;
            edits.push({ start: base + at, end: base + at + full.length, inserted: link.length });
            return link;
          }
          // Unresolved `#<...>`: escape `<` so CommonMark renders it as literal
          // plain text instead of dropping `<ses...>` as raw inline HTML.
          const escaped = `${trigger}\\<${inner}>`;
          edits.push({ start: base + at, end: base + at + full.length, inserted: escaped.length });
          return escaped;
        }
        const marker = parseAtMarker(inner);
        if (marker.target === 'bot') {
          const handle = marker.name.trim();
          if (botRefHandles.has(handle)) {
            const label = escapeMarkdownLabel(`@${botNames.get(handle) || handle}`);
            const link = `[${label}](${MENTION_LINK_SCHEME}:bot:${encodeURIComponent(handle)})`;
            edits.push({ start: base + at, end: base + at + full.length, inserted: link.length });
            return link;
          }
        } else if (agentRefNames.has(marker.name)) {
          const label = escapeMarkdownLabel(`@${marker.name}`);
          const link = `[${label}](${MENTION_LINK_SCHEME}:agent:${encodeURIComponent(marker.name)})`;
          edits.push({ start: base + at, end: base + at + full.length, inserted: link.length });
          return link;
        }
        const escaped = `${trigger}\\<${inner}>`;
        edits.push({ start: base + at, end: base + at + full.length, inserted: escaped.length });
        return escaped;
      },
    );

  const rewriteLine = agentReply ? rewriteAgent : rewriteLegacy;

  const isBlankPhysicalLine = (lineStart: number): boolean => {
    const nl = text.indexOf('\n', lineStart);
    const lineEnd = nl < 0 ? text.length : nl;
    for (let i = lineStart; i < lineEnd; i += 1) {
      const ch = text[i];
      if (ch !== ' ' && ch !== '\t' && ch !== '\r') return false;
    }
    return true;
  };

  // CommonMark indented-code block state tracked across the full text:
  // an indented line (4+ spaces or tab) is code ONLY when it continues an
  // indented code block or follows a blank line / start of text whose last
  // non-blank line was NOT a list item or list continuation, and its own
  // content does not start with a list marker.
  let afterBlank = true;
  let inIndentedCode = false;
  let lastNonBlankWasList = false;
  let currentLineIsCode = false;

  const rewrite = (segment: string, base: number): string => {
    let at = base;
    return segment
      .split('\n')
      .map((line, index) => {
        const lineAt = at;
        at += line.length + 1;
        const atLineStart = index > 0 || base === 0 || text[base - 1] === '\n';
        if (atLineStart) {
          if (isBlankPhysicalLine(lineAt)) {
            afterBlank = true;
            currentLineIsCode = false;
            return line;
          }
          if (INDENTED_CODE_LINE_RE.test(line)) {
            if (LIST_MARKER_RE.test(line.trimStart())) {
              inIndentedCode = false;
              afterBlank = false;
              lastNonBlankWasList = true;
              currentLineIsCode = false;
            } else if (inIndentedCode || (afterBlank && !lastNonBlankWasList)) {
              inIndentedCode = true;
              afterBlank = false;
              currentLineIsCode = true;
              return line;
            } else {
              inIndentedCode = false;
              afterBlank = false;
              currentLineIsCode = false;
            }
          } else {
            inIndentedCode = false;
            afterBlank = false;
            lastNonBlankWasList = LIST_ITEM_LINE_RE.test(line);
            currentLineIsCode = false;
          }
        } else if (currentLineIsCode) {
          return line;
        }
        return rewriteLine(line, lineAt);
      })
      .join('\n');
  };

  // Escape bracketed `<…>` markers inside a skipped Markdown link label in
  // agentReply mode so CommonMark does not swallow `<ses...>` as raw inline
  // HTML while keeping the outer link intact and leaving code spans untouched.
  const escapeLinkLabel = (label: string, base: number): string => {
    const escapeSegment = (seg: string, segBase: number): string =>
      seg.replace(MENTION_MARKER_RE, (full: string, trigger: string, inner: string, at: number) => {
        const escaped = `${trigger}\\<${inner}>`;
        edits.push({ start: segBase + at, end: segBase + at + full.length, inserted: escaped.length });
        return escaped;
      });
    let out = '';
    let cursor = 0;
    INLINE_CODE_SPAN_RE.lastIndex = 0;
    for (let cm = INLINE_CODE_SPAN_RE.exec(label); cm; cm = INLINE_CODE_SPAN_RE.exec(label)) {
      if (cm.index > cursor) {
        out += escapeSegment(label.slice(cursor, cm.index), base + cursor);
      }
      out += cm[0];
      cursor = cm.index + cm[0].length;
    }
    if (cursor < label.length) {
      out += escapeSegment(label.slice(cursor), base + cursor);
    }
    return out;
  };

  // Partition on code blocks/spans, autolinks, and links; rewrite only the
  // surrounding prose so markers in code and URLs stay literal.
  let result = '';
  let last = 0;
  SKIP_SEGMENT_RE.lastIndex = 0;
  for (let m = SKIP_SEGMENT_RE.exec(text); m; m = SKIP_SEGMENT_RE.exec(text)) {
    if (m.index > last) {
      result += rewrite(text.slice(last, m.index), last);
    }
    const seg = m[0];
    const linkLabel = m.groups?.linkLabel;
    const linkDest = m.groups?.linkDest;
    if (agentReply && linkLabel !== undefined && linkDest !== undefined && !currentLineIsCode) {
      result += escapeLinkLabel(linkLabel, m.index) + linkDest;
    } else {
      result += seg;
    }
    // Update block state only for line-start fenced blocks or inline skipped
    // segments beginning at column 0 — never for mid-line inline fences or
    // mid-line code spans, so enclosing indented code / list state is preserved.
    const isLineFence = m.groups?.lineFence !== undefined;
    if (isLineFence) {
      const fenceLine = seg.startsWith('\n') ? seg.slice(1) : seg;
      inIndentedCode = false;
      afterBlank = false;
      currentLineIsCode = false;
      if (!/^[ \t]/.test(fenceLine)) {
        lastNonBlankWasList = false;
      }
    } else if (m.index === 0 || text[m.index - 1] === '\n') {
      inIndentedCode = false;
      afterBlank = false;
      lastNonBlankWasList = false;
      currentLineIsCode = false;
    }
    last = m.index + seg.length;
  }
  if (last < text.length) {
    result += rewrite(text.slice(last), last);
  }
  return { text: result, edits };
}

/** Parse an `avibe-mention:<kind>:<value>` href back into its parts. */
export function parseMentionHref(href: string): { kind: MentionTrigger; target: MentionTarget; value: string } | null {
  const prefix = `${MENTION_LINK_SCHEME}:`;
  if (!href.startsWith(prefix)) return null;
  const rest = href.slice(prefix.length);
  const sep = rest.indexOf(':');
  if (sep < 0) return null;
  const kindStr = rest.slice(0, sep);
  let value: string;
  try {
    value = decodeURIComponent(rest.slice(sep + 1));
  } catch {
    value = rest.slice(sep + 1);
  }
  if (kindStr === 'agent') return { kind: '@', target: 'agent', value };
  if (kindStr === 'bot') return { kind: '@', target: 'bot', value };
  if (kindStr === 'session') return { kind: '#', target: 'session', value };
  return null;
}

/** True when the text contains at least one mention marker. */
export function hasMentionMarkers(text: string): boolean {
  MENTION_MARKER_RE.lastIndex = 0;
  return MENTION_MARKER_RE.test(text);
}

/** Replace resolved Session and Bot mentions with readable plain-text titles
 *  for single-line previews (such as a collapsed Harness bubble pill). Code
 *  spans/blocks, autolinks, and unresolved ids stay verbatim. */
export function formatMentionsPlainText(
  text: string,
  references?: MentionReference[],
  untitledLabel?: string,
): string {
  if (!text || !references?.length) return text;
  const sessionRefs = new Map<string, SessionReference>();
  const botNames = new Map<string, string>();
  const botHandles = new Set<string>();
  for (const ref of references) {
    if (ref.kind === 'session') {
      sessionRefs.set(ref.session_id, ref);
    } else if (ref.kind === 'bot') {
      botHandles.add(ref.handle);
      if (ref.display_name) botNames.set(ref.handle, ref.display_name);
    }
  }
  if (!sessionRefs.size && !botHandles.size) return text;

  const replaceSegment = (segment: string): string =>
    segment.replace(
      AGENT_MENTION_RE,
      (full: string, trigger: string | undefined, inner: string | undefined, bareSession: string | undefined) => {
        if (bareSession) {
          const ref = sessionRefs.get(bareSession);
          if (!ref || ref.openable === false) return full;
          return ref.title?.trim() || untitledLabel || bareSession;
        }
        if (!trigger || inner === undefined) return full;
        if (trigger === '#') {
          const sid = inner.trim();
          const ref = sessionRefs.get(sid);
          if (!ref || ref.openable === false) return full;
          return ref.title?.trim() || untitledLabel || sid;
        }
        const marker = parseAtMarker(inner);
        if (marker.target === 'bot') {
          const handle = marker.name.trim();
          if (botHandles.has(handle)) return `@${botNames.get(handle) || handle}`;
        }
        return full;
      },
    );

  let out = '';
  let last = 0;
  SKIP_SEGMENT_RE.lastIndex = 0;
  for (let m = SKIP_SEGMENT_RE.exec(text); m; m = SKIP_SEGMENT_RE.exec(text)) {
    if (m.index > last) out += replaceSegment(text.slice(last, m.index));
    out += m[0];
    last = m.index + m[0].length;
  }
  if (last < text.length) out += replaceSegment(text.slice(last));
  return out;
}
