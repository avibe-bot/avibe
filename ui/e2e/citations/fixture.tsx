import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Badge } from '../../src/components/ui/badge';
import { Markdown } from '../../src/components/ui/markdown';
import { bindCitations, bodyDigest, type CitationSource } from '../../src/lib/citations';
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

const SOURCES = [
  { index: 1, ref_id: 'turn0view0', title: 'Web search — OpenAI API', url: GUIDE, label: 'developers.openai.com' },
  { index: 2, ref_id: 'turn0view1', title: '引用探针来源 — Café', url: PROBE, label: 'example.com' },
  { index: 3, ref_id: 'turn0view2', title: '', url: REFERENCE, label: 'reference.invalid' },
] as const satisfies readonly CitationSource[];

// The answer as core.citations delivers it, assembled the way core.citations
// assembles it: a source in this list is a link the backend wrote, and it
// records the exact range it wrote it at. Everything else is the answer's own
// prose — including the sentence in its own words that points at the same page
// as source 1, which must stay an ordinary anchor no matter how closely it
// matches, and the marker grammar left literal inside a code example.
//
// Paragraph-separated on purpose: a cited line and an uncited one are then two
// comparable blocks, which is how the spec asserts that citing a source does not
// make the answer's lines taller.
const PARTS: Array<string | CitationSource> = [
  'Plain answer line without any citation.\n\n',
  'Native web search is documented. ', SOURCES[0], '\n\n',
  'Two sources, one unknown. ', SOURCES[1], ' (source unavailable)\n\n',
  'Three in a row. ', SOURCES[0], ' ', SOURCES[1], ' ', SOURCES[2], '\n\n',
  `In the answer's own words, see [the web search guide](${GUIDE}).\n\n`,
  'The marker grammar itself:\n\n',
  '```\n\uE200cite\uE202turn0view0\uE201\n```',
];

const build = () => {
  let text = '';
  const measured = new Map<CitationSource, number[][]>();
  for (const part of PARTS) {
    if (typeof part === 'string') {
      text += part;
      continue;
    }
    const start = text.length;
    text += `[${part.label}](${part.url})`;
    measured.set(part, [...(measured.get(part) ?? []), [start, text.length]]);
  }
  const body_sha256 = bodyDigest(text);
  return {
    text,
    citations: [...measured].map(([source, spans]) => ({ ...source, spans, body_sha256 })),
  };
};

const { text: ANSWER, citations } = build();
// ChatPage reads the sidecar against the body it was measured in and hands the
// renderer the result; the fixture goes through that same door.
const BINDING = bindCitations(citations, ANSWER);

export function Fixture() {
  return <div className="min-h-dvh bg-background p-4 text-foreground">
    <div className="mx-auto max-w-2xl space-y-4">
      <Badge variant="secondary">Reference badge</Badge>
      <div className="rounded-2xl border border-border bg-surface p-3 text-[13px] leading-relaxed">
        <Markdown content={ANSWER} citations={BINDING} className="vr-markdown--inherit-size" />
      </div>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><Fixture /></StrictMode>);
