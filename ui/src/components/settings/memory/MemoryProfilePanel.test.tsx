import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import type { MemoryProfile } from '../../../context/ApiContext';
import {
  ProfileReportAction,
  ProfileReportOutput,
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

  it('keeps hostile profile and report values inert while showing a report error separately', () => {
    const hostile: MemoryProfile = {
      ...PROFILE,
      summary: '<img src=x onerror=alert(1)>',
    };
    const html = renderToStaticMarkup(
      <>
        <StructuredMemoryProfile profile={hostile} t={t} />
        <ProfileReportOutput
          report={null}
          warning={null}
          error="memory_sidecar_unavailable"
          t={t}
        />
        <ProfileReportOutput
          report={'Overview\n\n<script>alert(1)</script>'}
          warning={null}
          error={null}
          t={t}
        />
      </>,
    );

    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).not.toContain('<img src=x');
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('memory_sidecar_unavailable');
    expect(html).toContain('memory.profile.reportTitle');
  });

  it('disables report generation until a structured profile is ready and shows the in-flight state', () => {
    const disabled = renderToStaticMarkup(
      <ProfileReportAction enabled={false} generating={false} onGenerate={() => undefined} t={t} />,
    );
    const generating = renderToStaticMarkup(
      <ProfileReportAction enabled={true} generating={true} onGenerate={() => undefined} t={t} />,
    );

    expect(disabled).toContain('disabled=""');
    expect(disabled).toContain('memory.profile.generateReport');
    expect(generating).toContain('disabled=""');
    expect(generating).toContain('memory.profile.generatingReport');
  });
});
