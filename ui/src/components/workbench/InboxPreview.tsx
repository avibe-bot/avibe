import { memo } from 'react';

import { Markdown } from '../ui/markdown';
import { inboxPreviewSource } from '../../lib/inboxPreview';

// The latest reply as an Inbox card shows it: a few line-clamped lines of
// Markdown. The reply itself can be many kilobytes (code, tables, tool output),
// and parsing all of it for every card made each live update of the list, and
// each switch of its tab, a long task. So a card parses only the part its clamp
// can show, and parses it again only when that text changes: not when the list
// around it changes, and not when the card is read and only its colour does.
const PreviewMarkdown = memo(function PreviewMarkdown({ text }: { text: string }) {
  return <Markdown content={inboxPreviewSource(text)} interactive={false} className="vr-markdown--preview" />;
});

/** One card's preview. */
export function InboxPreview({ text, className }: { text: string; className: string }) {
  return (
    <div className={className}>
      <PreviewMarkdown text={text} />
    </div>
  );
}
