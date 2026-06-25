import * as React from 'react';
import ReactMarkdown, { type Components, defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';

import { Badge } from '@/components/ui/badge';
import { ChatImage, LinkedImageProvider } from '@/components/ui/chat-image';
import { FileCard } from '@/components/ui/file-card';
import { SecretRequestCard } from '@/components/ui/secret-request-card';
import { isProxyMediaUrl, readMediaDims } from '@/lib/mediaProxy';
import {
  MENTION_LINK_SCHEME,
  linkifyMentions,
  parseMentionHref,
  type MentionReference,
} from '@/lib/mentions';
import { cn } from '@/lib/utils';

// Shared markdown renderer. react-markdown + remark-gfm (tables, strikethrough,
// task lists, autolinks); the element styling lives in index.css under
// ``.vr-markdown`` because the project doesn't ship the Tailwind typography
// plugin. Promoted out of ChatPage once a second caller (the agent-config
// editor preview) needed the same renderer — one home for "render markdown the
// Vibe Remote way", so the security-conscious <img> handling is shared too.
// ``interactive`` (default true) keeps the normal chat/editor rendering where
// links and image-links are clickable. Pass ``interactive={false}`` for snippets
// that live inside a clickable row/button (e.g. inbox previews): links render as
// plain text so a nested <a> can't become invalid interactive content or steal
// the row's click.
// ``softBreaks`` (default false) turns a single newline into a hard <br> via
// remark-breaks — CommonMark otherwise collapses a soft break to a space. Enable
// it for as-typed text (the user's own chat messages) so a multi-line prompt
// echoes with its line breaks intact while still formatting explicit markdown;
// leave it off for authored markdown (agent replies) where wrapped lines must
// not sprout stray hard breaks.

// Vault dynamic-ask: an agent reply may contain `$<OPENAI_API_KEY>`. When interactive,
// we rewrite each marker (outside code) to an `avibe-secret:NAME` link that the `a` handler
// renders as an inline SecretRequestCard. Fenced/inline code spans AND indented (≥4-space / tab)
// code lines are left verbatim so a marker shown in any code example never becomes a real
// input card.
const SECRET_LINK_SCHEME = 'avibe-secret';
const SECRET_REQUEST_RE = /\$<([A-Z][A-Z0-9_]*)>/g;
const CODE_SPAN_RE = /```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`/g;
const INDENTED_CODE_LINE_RE = /^(?: {4}|\t)/;

function linkifySecretRequests(text: string): string {
  if (!text.includes('$<')) return text;
  // Within a non-fenced/non-inline-code segment, still skip indented code lines.
  const rewrite = (segment: string) =>
    segment
      .split('\n')
      .map((line) =>
        INDENTED_CODE_LINE_RE.test(line)
          ? line
          : line.replace(SECRET_REQUEST_RE, (_m, name) => `[${name}](${SECRET_LINK_SCHEME}:${name})`),
      )
      .join('\n');
  // Partition on code spans; rewrite only the non-code segments (code stays verbatim).
  let result = '';
  let last = 0;
  CODE_SPAN_RE.lastIndex = 0;
  for (let m = CODE_SPAN_RE.exec(text); m; m = CODE_SPAN_RE.exec(text)) {
    result += rewrite(text.slice(last, m.index)) + m[0];
    last = m.index + m[0].length;
  }
  return result + rewrite(text.slice(last));
}

// Keep react-markdown's URL sanitizer from stripping our custom schemes (it allows
// only http/https/mailto/tel/relative by default).
function mentionUrlTransform(url: string): string {
  if (url.startsWith(`${MENTION_LINK_SCHEME}:`) || url.startsWith(`${SECRET_LINK_SCHEME}:`)) return url;
  return defaultUrlTransform(url);
}

export const Markdown: React.FC<{
  content: string;
  className?: string;
  interactive?: boolean;
  softBreaks?: boolean;
  /** Mention sidecar — when present, `@<name>` / `#<id>` markers in `content`
   *  render as inline chips (see lib/mentions). */
  references?: MentionReference[];
  /** Opt-in: render `$<NAME>` dynamic-ask markers as interactive SecretRequestCards. ONLY the
   *  agent-reply surface sets this — user bubbles, previews, and docs must NOT, or user-authored
   *  or quoted text could mint an "agent asked for this secret" card that creates a vault
   *  secret on click. */
  secretRequests?: boolean;
}> = ({ content, className, interactive = true, softBreaks = false, references, secretRequests = false }) => {
  // Stable ``remarkPlugins`` + ``components`` identities across re-renders.
  // ReactMarkdown keys its rendered tree on the component functions it is handed;
  // the old inline object minted fresh functions every render, so ReactMarkdown
  // treated each custom <img>/<a> as a NEW component type and REMOUNTED the whole
  // subtree. A remounted <img> is re-fetched / re-decoded — which is exactly what
  // makes a chat bubble's image FLICKER on every scroll-triggered re-render in iOS
  // Safari (the box is already reserved, so it isn't a layout shift; the bitmap
  // itself blinks). Memoizing lets ReactMarkdown reconcile the existing nodes in
  // place. (MessageRow is also React.memo'd so a scroll re-render of the transcript
  // never reaches here to begin with — this is defence in depth + correctness for
  // the editor-preview caller that lacks that wrapper.)
  const remarkPlugins = React.useMemo(
    () => (softBreaks ? [remarkGfm, remarkBreaks] : [remarkGfm]),
    [softBreaks],
  );
  const components = React.useMemo<Components>(
    () => ({
      // Markdown here is untrusted (agent replies, user-authored prompts) and
      // can embed images. The default <img> renderer would auto-fetch any URL
      // the moment the view opens (``![](http://attacker/x)``), leaking the
      // viewer's IP / network metadata to an attacker-chosen host. So we only
      // render a real inline <img> for our OWN same-origin media proxy; every
      // other URL stays a click-through link (or plain text when
      // non-interactive) so nothing is fetched without an explicit action.
      img: ({ src, alt }) => {
        if (!src) return null;
        const url = String(src);
        if (interactive && isProxyMediaUrl(url)) {
          // Pixel dimensions ride on the proxy URL (``?w=&h=``) so the image's
          // box is reserved before it loads — no scroll shift on the transcript.
          const { width, height } = readMediaDims(url);
          return <ChatImage src={url} alt={alt || ''} width={width} height={height} />;
        }
        const label = `🖼 ${alt || url}`;
        return interactive ? (
          <a href={url} target="_blank" rel="noopener noreferrer nofollow">
            {label}
          </a>
        ) : (
          <span>{label}</span>
        );
      },
      // Links to our media proxy are agent-produced files → render the
      // download card (filename + type + download / preview). Other links keep
      // the normal anchor (interactive) or collapse to plain text inside a
      // clickable row (non-interactive).
      a: ({ href, children }) => {
        const url = href ? String(href) : '';
        // @-agent / #-session mention chips (see lib/mentions). Rendered in both
        // interactive and non-interactive contexts — a chip is a span, safe inside
        // a clickable row, and never triggers a fetch or navigation.
        const mention = parseMentionHref(url);
        if (mention) {
          return (
            <Badge
              variant={mention.kind === '@' ? 'success' : 'info'}
              className="max-w-[18rem] truncate align-baseline font-medium"
            >
              {children}
            </Badge>
          );
        }
        // Vault `$<NAME>` dynamic-ask marker → inline secure-input card. Gated on
        // ``secretRequests`` (agent-reply surface only), NOT just ``interactive`` — otherwise a
        // user who literally typed `[x](avibe-secret:FOO)` in their own bubble could mint a card.
        if (url.startsWith(`${SECRET_LINK_SCHEME}:`)) {
          const name = url.slice(SECRET_LINK_SCHEME.length + 1);
          return secretRequests ? <SecretRequestCard name={name} /> : <span>{children}</span>;
        }
        if (interactive && url && isProxyMediaUrl(url)) {
          return <FileCard href={url}>{children}</FileCard>;
        }
        if (!interactive) return <span>{children}</span>;
        // Wrap children so a nested ChatImage (``[![](media)](href)``) renders
        // bare — without its own download anchor inside this one.
        return (
          <a href={url} target="_blank" rel="noopener noreferrer nofollow">
            <LinkedImageProvider>{children}</LinkedImageProvider>
          </a>
        );
      },
      ...(interactive
        ? {}
        : {
            // GFM task lists render a checkbox <input>; even disabled, an
            // <input> nested in the sidebar row <button> is invalid interactive
            // content, so show the state as a plain glyph instead.
            input: ({ checked }: { checked?: boolean }) => (
              <span aria-hidden="true">{checked ? '☑ ' : '☐ '}</span>
            ),
          }),
    }),
    [interactive, secretRequests],
  );

  // Mention markers are rewritten to `avibe-mention:` links BEFORE markdown sees
  // them, and only when a sidecar is present — agent replies (no references) skip
  // this so their code spans are never touched. The links render as chips via the
  // `a` map. Secret `$<NAME>` markers are rewritten too, but only on the agent-reply surface
  // (secretRequests) — user bubbles / previews / docs keep them as plain text.
  let rendered = references && references.length ? linkifyMentions(content, references) : content;
  if (secretRequests) rendered = linkifySecretRequests(rendered);
  return (
    <div className={cn('vr-markdown', className)}>
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        components={components}
        urlTransform={mentionUrlTransform}
      >
        {rendered}
      </ReactMarkdown>
    </div>
  );
};
