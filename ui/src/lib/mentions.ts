// Shared contract for @-mentions (Bots and Agents) and #-session references in
// the chat composer.
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
  /** Snapshot of the title at send time — the chip label. */
  title?: string | null;
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
  return value.replace(/[\\[\]]/g, (m) => `\\${m}`).replace(/\s*\n\s*/g, ' ');
}

/**
 * Rewrite `@<…>` / `#<…>` markers in `text` into markdown links carrying the
 * `avibe-mention:` scheme, so the shared `Markdown` renderer can show them as
 * chips via its `a` component map. Session labels use the title from
 * `references` when available, else the raw id.
 *
 * A marker becomes a link of a different length, which moves everything after
 * it — including any source citation the backend measured in this same text.
 * So each rewrite is reported as a `TextEdit` in the coordinates of `text`,
 * letting the caller carry those measurements across this pass (see
 * lib/citations). The rewritten text itself is unchanged by that bookkeeping.
 */
// Inline code spans and fenced code blocks — markers inside these must render
// literally (e.g. `` `@<T>` ``), so linkify skips them.
const CODE_SEGMENT_RE = /(```[\s\S]*?```|`[^`]*`)/g;

export function linkifyMentions(text: string, references?: MentionReference[]): EditedText {
  const sessionTitles = new Map<string, string>();
  const botNames = new Map<string, string>();
  for (const ref of references ?? []) {
    if (ref.kind === 'session' && ref.title) sessionTitles.set(ref.session_id, ref.title);
    if (ref.kind === 'bot' && ref.display_name) botNames.set(ref.handle, ref.display_name);
  }
  const edits: TextEdit[] = [];
  const rewrite = (segment: string, base: number): string =>
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
        label = escapeMarkdownLabel(`#${sessionTitles.get(inner) || inner}`);
      }
      const link = `[${label}](${MENTION_LINK_SCHEME}:${kind}:${encodeURIComponent(value)})`;
      edits.push({ start: base + at, end: base + at + full.length, inserted: link.length });
      return link;
    });
  // Split out code spans/blocks (the odd capture-group chunks) and rewrite markers
  // only in the surrounding prose, so marker-shaped text inside code stays literal.
  // The chunks concatenate back to `text`, so a running cursor over them is each
  // chunk's own offset in the input.
  let cursor = 0;
  const rewritten = text
    .split(CODE_SEGMENT_RE)
    .map((chunk) => {
      const base = cursor;
      cursor += chunk.length;
      return chunk.startsWith('`') ? chunk : rewrite(chunk, base);
    })
    .join('');
  return { text: rewritten, edits };
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
