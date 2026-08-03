import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import type { MemoryProfile, MemoryProfilePageDescriptor } from '../../../context/ApiContext';
import {
  ProfilePageOutput,
  ProfileReportAction,
  StructuredMemoryProfile,
  structuredProfileFromItems,
} from './MemoryProfilePanel';

const t = (key: string) => key;

const PROFILE: MemoryProfile = {
  summary: 'Prefers concise technical updates.',
  explicit_info: [
    {
      category: 'communication',
      description: 'Prefers written updates.',
      evidence: 'Asked for a written summary.',
    },
  ],
  implicit_traits: [
    {
      trait: 'methodical',
      description: 'May prefer a clear sequence of steps.',
      basis: 'Repeatedly requested checklists.',
      evidence: 'Several planning discussions.',
    },
  ],
  updated_at: '2026-08-02T10:30:00Z',
};

const PAGE: MemoryProfilePageDescriptor = {
  artifact_id: 'a'.repeat(32),
  language: 'en',
  generated_at: '2026-08-03T05:12:30Z',
  published_at: '2026-08-03T05:12:31Z',
  source_profile_updated_at: '2026-08-02T10:30:00Z',
  source_profile_snapshot_id: `sha256:${'b'.repeat(64)}`,
  prompt_contract_version: 2,
  content_sha256: `sha256:${'c'.repeat(64)}`,
  view_url: `/api/memory/profile/report/view/en/${'a'.repeat(32)}/index.html`,
};

describe('MemoryProfilePanel deterministic content', () => {
  it('renders structured sections and keeps basis distinct from evidence', () => {
    const html = renderToStaticMarkup(<StructuredMemoryProfile profile={PROFILE} t={t} />);

    expect(html).toContain('memory.profile.summary');
    expect(html).toContain('memory.profile.explicitInfo');
    expect(html).toContain('memory.profile.implicitTraits');
    expect(html).toContain('Repeatedly requested checklists.');
    expect(html).toContain('Several planning discussions.');
    expect(html).toContain('memory.profile.basis');
    expect(html).toContain('memory.profile.evidence');
  });

  it('falls back only when a profile item has no structured profile field', () => {
    expect(
      structuredProfileFromItems([
        { kind: 'profile', text: '{"legacy":true}', date: null },
        { kind: 'fact', text: 'A fact', date: null },
      ]),
    ).toBeNull();
    expect(
      structuredProfileFromItems([{ kind: 'profile', text: '{}', date: null, profile: PROFILE }]),
    ).toBe(PROFILE);
  });

  it('keeps hostile profile values inert and isolates a durable page in a sandboxed iframe', () => {
    const hostile: MemoryProfile = {
      ...PROFILE,
      summary: '<img src=x onerror=alert(1)>',
    };
    const html = renderToStaticMarkup(
      <>
        <StructuredMemoryProfile profile={hostile} t={t} />
        <ProfilePageOutput
          page={PAGE}
          freshness="stale"
          loading={false}
          generating={true}
          warning={null}
          error="memory_sidecar_unavailable"
          onOpen={() => undefined}
          t={t}
        />
      </>,
    );

    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).not.toContain('<img src=x');
    expect(html).toContain('memory_sidecar_unavailable');
    expect(html).toContain('memory.profile.pageTitle');
    expect(html).toContain('memory.profile.pageFreshness.stale');
    expect(html).toContain('memory.profile.generatingPage');
    expect(html).toContain('sandbox=""');
    expect(html).toContain(PAGE.view_url);
    expect(html).toContain(PAGE.generated_at);
    expect(html).toContain(PAGE.source_profile_updated_at);
  });

  it('disables report generation until a structured profile is ready and shows the in-flight state', () => {
    const disabled = renderToStaticMarkup(
      <ProfileReportAction enabled={false} generating={false} onGenerate={() => undefined} t={t} />,
    );
    const generating = renderToStaticMarkup(
      <ProfileReportAction enabled={true} generating={true} onGenerate={() => undefined} t={t} />,
    );

    expect(disabled).toContain('disabled=""');
    expect(disabled).toContain('memory.profile.generatePage');
    expect(generating).toContain('disabled=""');
    expect(generating).toContain('memory.profile.generatingPage');
  });
});
