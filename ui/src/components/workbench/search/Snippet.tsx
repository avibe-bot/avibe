import type { MessageSnippet } from '../../../context/ApiContext';

type SnippetProps = {
  snippet: MessageSnippet;
};

// A single, truncated line of message text with the matched term highlighted.
// The server pre-splits the window into prefix / match / suffix, so this is
// purely presentational. The <mark> uses the design's gold highlight
// (bg-gold/10 + text-gold — the Badge "warning" tone; there is no gold-soft
// token) with a small radius, no underline. When ``match`` is empty (leading
// context only) we render just the prefix.
export const Snippet: React.FC<SnippetProps> = ({ snippet }) => {
  const { prefix, match, suffix } = snippet;
  return (
    <span className="block truncate text-[12.5px] leading-relaxed text-foreground">
      {prefix}
      {match && (
        <mark className="rounded-md bg-gold/10 px-1 font-medium text-gold no-underline">
          {match}
        </mark>
      )}
      {match ? suffix : ''}
    </span>
  );
};
