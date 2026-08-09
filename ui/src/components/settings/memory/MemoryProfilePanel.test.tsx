import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import type { MemoryProfile } from '../../../context/ApiContext';
import { StructuredMemoryProfile } from './MemoryProfilePanel';
import { structuredProfileFromItems } from './memoryProfile';

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

describe('MemoryProfilePanel structured text', () => {
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

  it('keeps hostile profile values as inert text', () => {
    const html = renderToStaticMarkup(
      <StructuredMemoryProfile
        profile={{ ...PROFILE, summary: '<img src=x onerror=alert(1)>' }}
        t={t}
      />,
    );

    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).not.toContain('<img src=x');
  });
});
