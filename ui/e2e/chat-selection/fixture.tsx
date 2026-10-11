import { StrictMode, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { Markdown } from '../../src/components/ui/markdown';
import { SelectionQuoteToolbar } from '../../src/components/workbench/SelectionQuoteToolbar';
import '../../src/i18n';
import '../../src/index.css';

// Two transcript bubbles rendered by the production Markdown renderer under the
// production selection toolbar, with a timestamp between them that is not
// Markdown. What this fixture exists to check is the Range a real browser makes
// for a double-click, a triple-click, or a drag, which jsdom cannot produce.
const BUBBLES = [
  [
    'Before **bold words** after.',
    '',
    'Second paragraph with `code`.',
    '',
    '- first item',
    '- second item',
  ].join('\n'),
  '前面 **加粗文字** 后面',
];

export function Fixture() {
  const containerRef = useRef<HTMLDivElement>(null);
  return (
    <div ref={containerRef} className="min-h-dvh space-y-4 bg-background p-4 text-[15px] text-foreground">
      <div className="max-w-xl rounded-2xl border border-border bg-surface p-3">
        <Markdown content={BUBBLES[0]} />
      </div>
      <span className="text-muted">12:00</span>
      <div className="max-w-xl rounded-2xl border border-border bg-surface p-3">
        <Markdown content={BUBBLES[1]} />
      </div>
      <SelectionQuoteToolbar containerRef={containerRef} onQuote={() => undefined} />
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<StrictMode><Fixture /></StrictMode>);
