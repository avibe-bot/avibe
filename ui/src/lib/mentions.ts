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

// Segments skipped verbatim during mention rewriting: fenced code blocks
// (closed or still-open while streaming), inline code spans, autolinks
// (`<https://...>`), full Markdown links (`[label](dest)` — so mentions inside
// an existing link label never create nested links), and bare link destinations
// (`](dest)` — fallback when a link label itself contains nested brackets).
const SKIP_SEGMENT_RE =
  /```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|(`+)[^`\n]+?\1|<https?:\/\/[^>\s]+>|\[[^\]\n]*\]\((?:[^()\s]|(?:\([^()\s]*\)))*\)|\]\((?:[^()\s]|(?:\([^()\s]*\)))*\)/g;

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
  const rewrite = (segment: string, base: number): string => {
    let at = base;
    return segment
      .split('\n')
      .map((line, index) => {
        const lineAt = at;
        at += line.length + 1;
        const atLineStart = index > 0 || base === 0 || text[base - 1] === '\n';
        if (atLineStart && INDENTED_CODE_LINE_RE.test(line)) return line;
        return rewriteLine(line, lineAt);
      })
      .join('\n');
  };

  // Partition on code blocks/spans, autolinks, and link destinations; rewrite
  // only the surrounding prose so markers in code and URLs stay literal.
  let result = '';
  let last = 0;
  SKIP_SEGMENT_RE.lastIndex = 0;
  for (let m = SKIP_SEGMENT_RE.exec(text); m; m = SKIP_SEGMENT_RE.exec(text)) {
    if (m.index > last) {
      result += rewrite(text.slice(last, m.index), last);
    }
    result += m[0];
    last = m.index + m[0].length;
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
