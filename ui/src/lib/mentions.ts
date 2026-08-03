// Shared contract for @-agent / #-session mentions in the chat composer.
//
// Marker convention (the agent-facing, transport-stable form): `@<agent-name>`
// and `#<session-id>` live inline in the message text. A structured sidecar in
// `message.content.references` carries the resolved data (ids, session titles)
// for faithful chip rendering and backend context — see docs/plans.
//
// Humans never read the raw markers (the bubble renders chips), so the format is
// optimized for LLM legibility + robust parsing. The `<…>` form is excised from
// the text BEFORE react-markdown sees it (we rewrite markers to mention links),
// so it never collides with markdown's `<tag>` HTML handling.

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

export type MentionReference = AgentReference | SessionReference;

export const MENTION_TRIGGERS = ['@', '#'] as const;
export type MentionTrigger = (typeof MENTION_TRIGGERS)[number];

// Matches `@<…>` / `#<…>`. Inner text excludes `>` and newlines: agent names with
// `>` are disallowed (enforced at insert time) and session ids are token-safe.
export const MENTION_MARKER_RE = /([@#])<([^>\n]+)>/g;

/** The custom link scheme markers are rewritten to before markdown rendering. */
export const MENTION_LINK_SCHEME = 'avibe-mention';

/** The inline text marker for a reference. */
export function referenceToMarker(ref: MentionReference): string {
  return ref.kind === 'agent' ? `@<${ref.name}>` : `#<${ref.session_id}>`;
}

/** Dedupe references by (kind, id) so a marker repeated in the text yields one entry. */
export function dedupeReferences(refs: MentionReference[]): MentionReference[] {
  const seen = new Set<string>();
  const out: MentionReference[] = [];
  for (const ref of refs) {
    const key = ref.kind === 'agent' ? `agent:${ref.name}` : `session:${ref.session_id}`;
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
 */
// Inline code spans and fenced code blocks — markers inside these must render
// literally (e.g. `` `@<T>` ``), so linkify skips them.
const CODE_SEGMENT_RE = /(```[\s\S]*?```|`[^`]*`)/g;

export function linkifyMentions(text: string, references?: MentionReference[]): string {
  const sessionTitles = new Map<string, string>();
  for (const ref of references ?? []) {
    if (ref.kind === 'session' && ref.title) sessionTitles.set(ref.session_id, ref.title);
  }
  const rewrite = (segment: string): string =>
    segment.replace(MENTION_MARKER_RE, (_full, trigger: string, inner: string) => {
      if (trigger === '@') {
        const label = escapeMarkdownLabel(`@${inner}`);
        return `[${label}](${MENTION_LINK_SCHEME}:agent:${encodeURIComponent(inner)})`;
      }
      const label = escapeMarkdownLabel(`#${sessionTitles.get(inner) || inner}`);
      return `[${label}](${MENTION_LINK_SCHEME}:session:${encodeURIComponent(inner)})`;
    });
  // Split out code spans/blocks (the odd capture-group chunks) and rewrite markers
  // only in the surrounding prose, so marker-shaped text inside code stays literal.
  return text
    .split(CODE_SEGMENT_RE)
    .map((chunk) => (chunk.startsWith('`') ? chunk : rewrite(chunk)))
    .join('');
}

/** Parse an `avibe-mention:<kind>:<value>` href back into its parts. */
export function parseMentionHref(href: string): { kind: MentionTrigger; value: string } | null {
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
  if (kindStr === 'agent') return { kind: '@', value };
  if (kindStr === 'session') return { kind: '#', value };
  return null;
}

/** True when the text contains at least one mention marker. */
export function hasMentionMarkers(text: string): boolean {
  MENTION_MARKER_RE.lastIndex = 0;
  return MENTION_MARKER_RE.test(text);
}
