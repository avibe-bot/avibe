import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Badge } from '../../src/components/ui/badge';
import { Markdown } from '../../src/components/ui/markdown';
import type { CitationSource } from '../../src/lib/citations';
import '../../src/i18n';
import '../../src/index.css';

// The production Markdown renderer with a citation sidecar, exactly as the
// agent-reply bubble passes it (ChatPage: agent-authored rows only). Nothing is
// mocked but the message itself — real CSS, real Popover, real i18n — because
// what this fixture exists to check is browser layout and pointer routing, which
// jsdom cannot answer.
const GUIDE = 'https://developers.openai.com/api/docs/guides/tools-web-search';
const PROBE = 'https://example.com/citation-probe-source';
const REFERENCE = 'https://reference.invalid/three';

// `occurrences` / `occurrence_total` are what core.citations records: which
// links in this exact text each citation wrote, among the links sharing its
// destination. Source 1 wrote two of the three links pointing at GUIDE — the
// third is the sentence the answer worded itself, which must stay an ordinary
// anchor no matter how closely it matches.
const sources: CitationSource[] = [
  { index: 1, ref_id: 'turn0view0', title: 'Web search — OpenAI API', url: GUIDE, label: 'developers.openai.com', occurrences: [1, 2], occurrence_total: 3 },
  { index: 2, ref_id: 'turn0view1', title: '引用探针来源 — Café', url: PROBE, label: 'example.com', occurrences: [1, 2], occurrence_total: 2 },
  { index: 3, ref_id: 'turn0view2', title: '', url: REFERENCE, label: 'reference.invalid', occurrences: [1], occurrence_total: 1 },
];
const cite = (source: CitationSource) => `[${source.label}](${source.url})`;

// Post-resolution text, as core.citations writes it: ordinary Markdown links,
// an unresolved ref downgraded to a visible label, and the grammar itself left
// literal inside a code example.
// Paragraph-separated on purpose: a cited line and an uncited one are then two
// comparable blocks, which is how the spec asserts that citing a source does not
// make the answer's lines taller.
const ANSWER = [
  'Plain answer line without any citation.',
  '',
  `Native web search is documented. ${cite(sources[0])}`,
  '',
  `Two sources, one unknown. ${cite(sources[1])} (source unavailable)`,
  '',
  `Three in a row. ${cite(sources[0])} ${cite(sources[1])} ${cite(sources[2])}`,
  '',
  `In the answer's own words, see [the web search guide](${GUIDE}).`,
  '',
  'The marker grammar itself:',
  '',
  '```',
  '\uE200cite\uE202turn0view0\uE201',
  '```',
].join('\n');

export function Fixture() {
  return <div className="min-h-dvh bg-background p-4 text-foreground">
    <div className="mx-auto max-w-2xl space-y-4">
      <Badge variant="secondary">Reference badge</Badge>
      <div className="rounded-2xl border border-border bg-surface p-3 text-[13px] leading-relaxed">
        <Markdown content={ANSWER} citations={sources} className="vr-markdown--inherit-size" />
      </div>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><Fixture /></StrictMode>);
