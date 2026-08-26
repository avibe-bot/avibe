/* @vitest-environment jsdom */

import { renderToStaticMarkup } from 'react-dom/server';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MemoryProfile } from '../../../context/ApiContext';
import { MemoryProfileItemBlock, MemoryProfilePanel, StructuredMemoryProfile } from './MemoryProfilePanel';
import { structuredProfileFromItems } from './memoryProfile';

const t = (key: string) => key;
const getMemoryProfile = vi.hoisted(() => vi.fn());

vi.mock('../../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../../context/ApiContext')>();
  return { ...original, useApi: () => ({ getMemoryProfile }) };
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t }),
}));

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

beforeEach(() => {
  getMemoryProfile.mockResolvedValue({ status: 'ok', items: [], warnings: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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

  it('labels user and Agent profile blocks while legacy blocks still render', () => {
    const html = renderToStaticMarkup(
      <>
        <MemoryProfileItemBlock item={{ kind: 'profile', text: 'User profile', date: null, origin: 'user' }} t={t} />
        <MemoryProfileItemBlock item={{ kind: 'profile', text: 'Agent profile', date: null, origin: 'agent' }} t={t} />
        <MemoryProfileItemBlock item={{ kind: 'profile', text: 'Legacy profile', date: null }} t={t} />
      </>,
    );

    expect(html).toContain('memory.origin.user');
    expect(html).toContain('memory.origin.agent');
    expect(html).toContain('Legacy profile');
  });

  it('renders a partial warning while retaining the successful owner profile', async () => {
    getMemoryProfile.mockResolvedValue({
      status: 'ok',
      items: [{ kind: 'profile', text: 'Available user profile', date: null, origin: 'user' }],
      warnings: ['memory_search_partial'],
    });

    render(<MemoryProfilePanel enabled />);

    expect(await screen.findByText('memory.profile.partial')).toBeTruthy();
    expect(screen.getByText('Available user profile')).toBeTruthy();
  });

  it('does not call a partially unread empty profile ungenerated', async () => {
    getMemoryProfile.mockResolvedValue({
      status: 'ok',
      items: [],
      warnings: ['memory_search_partial'],
      profile_warning: 'empty',
    });

    render(<MemoryProfilePanel enabled />);

    expect(await screen.findByText('memory.profile.partial')).toBeTruthy();
    expect(screen.queryByText('memory.profile.warningEmpty')).toBeNull();
    expect(screen.queryByText('memory.profile.empty')).toBeNull();
  });
});
