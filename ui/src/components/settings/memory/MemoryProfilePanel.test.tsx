/* @vitest-environment jsdom */

import { renderToStaticMarkup } from 'react-dom/server';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MemoryProfile } from '../../../context/ApiContext';
import { MemoryProfileItemBlock, MemoryProfilePanel, StructuredMemoryProfile } from './MemoryProfilePanel';
import { structuredProfileFromItems } from './memoryProfile';

const t = (key: string, options?: Record<string, unknown>) => {
  if (key === 'memory.profile.entryFallback') return `Profile entry ${options?.number}`;
  return key;
};
const api = vi.hoisted(() => ({ getMemoryProfile: vi.fn() }));
const { getMemoryProfile } = api;

vi.mock('../../../context/ApiContext', () => ({
  useApi: () => api,
}));

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
  it('renders headers and summary eagerly, but keeps entries collapsed until opened', async () => {
    render(<StructuredMemoryProfile profile={PROFILE} t={t} />);

    expect(screen.getByText('memory.profile.summary')).toBeTruthy();
    expect(screen.getByText('Prefers concise technical updates.')).toBeTruthy();
    expect(screen.getByText('memory.profile.explicitInfo')).toBeTruthy();
    expect(screen.getByText('memory.profile.implicitTraits')).toBeTruthy();

    // Closed by default: labels are visible, but body content is not rendered at all.
    expect(screen.getByRole('button', { name: 'communication' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'methodical' })).toBeTruthy();
    expect(screen.queryByText('Prefers written updates.')).toBeNull();
    expect(screen.queryByText('May prefer a clear sequence of steps.')).toBeNull();
    expect(screen.queryByText('Repeatedly requested checklists.')).toBeNull();
    expect(screen.queryByText('Asked for a written summary.')).toBeNull();
    expect(screen.queryByText('Several planning discussions.')).toBeNull();

    // Expanding the trait row reveals description and basis, but not evidence yet
    // (opening a row must not auto-open its evidence disclosure).
    await userEvent.click(screen.getByRole('button', { name: 'methodical' }));
    expect(screen.getByText('May prefer a clear sequence of steps.')).toBeTruthy();
    expect(screen.getByText(/memory\.profile\.basis/)).toBeTruthy();
    expect(screen.getByText('Repeatedly requested checklists.')).toBeTruthy();
    expect(screen.queryByText('Several planning discussions.')).toBeNull();

    // Its evidence disclosure is independent and stays basis-distinct once opened.
    await userEvent.click(screen.getByRole('button', { name: 'memory.profile.evidence' }));
    expect(screen.getByText('Several planning discussions.')).toBeTruthy();
    expect(screen.getByText('Repeatedly requested checklists.')).toBeTruthy();
  });

  it('toggles an entry open and closed via its title trigger, with correct aria-expanded', async () => {
    render(<StructuredMemoryProfile profile={PROFILE} t={t} />);
    const trigger = screen.getByRole('button', { name: 'communication' });

    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('Prefers written updates.')).toBeTruthy();

    await userEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Prefers written updates.')).toBeNull();
  });

  it('is keyboard accessible: Enter and Space both activate the disclosure trigger', async () => {
    render(<StructuredMemoryProfile profile={PROFILE} t={t} />);
    const trigger = screen.getByRole('button', { name: 'communication' });
    trigger.focus();

    await userEvent.keyboard('{Enter}');
    expect(trigger.getAttribute('aria-expanded')).toBe('true');

    await userEvent.keyboard(' ');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
  });

  it('keeps evidence independently collapsed after its entry opens, and closing evidence keeps the entry open', async () => {
    render(<StructuredMemoryProfile profile={PROFILE} t={t} />);
    await userEvent.click(screen.getByRole('button', { name: 'communication' }));
    expect(screen.queryByText('Asked for a written summary.')).toBeNull();

    const evidenceTrigger = screen.getByRole('button', { name: 'memory.profile.evidence' });
    await userEvent.click(evidenceTrigger);
    expect(screen.getByText('Asked for a written summary.')).toBeTruthy();

    await userEvent.click(evidenceTrigger);
    expect(screen.queryByText('Asked for a written summary.')).toBeNull();
    expect(screen.getByText('Prefers written updates.')).toBeTruthy();
  });

  it('toggles sibling entries independently', async () => {
    const twoEntryProfile: MemoryProfile = {
      summary: null,
      explicit_info: [
        { category: 'communication', description: 'Desc A', evidence: null },
        { category: 'timezone', description: 'Desc B', evidence: null },
      ],
      implicit_traits: [],
      updated_at: null,
    };
    render(<StructuredMemoryProfile profile={twoEntryProfile} t={t} />);

    const first = screen.getByRole('button', { name: 'communication' });
    const second = screen.getByRole('button', { name: 'timezone' });

    await userEvent.click(first);
    expect(screen.getByText('Desc A')).toBeTruthy();
    expect(screen.queryByText('Desc B')).toBeNull();

    await userEvent.click(second);
    expect(screen.getByText('Desc A')).toBeTruthy();
    expect(screen.getByText('Desc B')).toBeTruthy();

    await userEvent.click(first);
    expect(screen.queryByText('Desc A')).toBeNull();
    expect(screen.getByText('Desc B')).toBeTruthy();
  });

  it('gives blank category/trait a deterministic numbered fallback, numbered independently per section', () => {
    const fallbackProfile: MemoryProfile = {
      summary: null,
      explicit_info: [
        { category: '   ', description: 'Desc A', evidence: null },
        { category: null, description: 'Desc B', evidence: null },
      ],
      implicit_traits: [{ trait: '', description: 'Trait A', basis: null, evidence: null }],
      updated_at: null,
    };
    render(<StructuredMemoryProfile profile={fallbackProfile} t={t} />);

    // Both sections start their own numbering at 1: explicit-info's first entry
    // and implicit-traits' only entry both fall back to "Profile entry 1".
    expect(screen.getAllByRole('button', { name: 'Profile entry 1' })).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: 'Profile entry 2' })).toHaveLength(1);
  });

  it('keeps a long Unicode title fully intact and wrappable, with no horizontal overflow styling', () => {
    const longTitle =
      '很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长的分类标题不应该发生横向溢出';
    const longTitleProfile: MemoryProfile = {
      summary: null,
      explicit_info: [{ category: longTitle, description: 'Desc', evidence: null }],
      implicit_traits: [],
      updated_at: null,
    };
    render(<StructuredMemoryProfile profile={longTitleProfile} t={t} />);

    const trigger = screen.getByRole('button', { name: longTitle });
    expect(trigger.textContent).toBe(longTitle);
    const badge = trigger.querySelector('span');
    expect(badge?.className).toContain('whitespace-normal');
    expect(badge?.className).toContain('break-words');
  });

  it('renders no evidence toggle when evidence is absent or empty', async () => {
    const noEvidenceProfile: MemoryProfile = {
      summary: null,
      explicit_info: [
        { category: 'no-evidence-null', description: 'Desc A', evidence: null },
        { category: 'no-evidence-empty', description: 'Desc B', evidence: '' },
      ],
      implicit_traits: [],
      updated_at: null,
    };
    render(<StructuredMemoryProfile profile={noEvidenceProfile} t={t} />);

    await userEvent.click(screen.getByRole('button', { name: 'no-evidence-null' }));
    await userEvent.click(screen.getByRole('button', { name: 'no-evidence-empty' }));

    expect(screen.queryByRole('button', { name: 'memory.profile.evidence' })).toBeNull();
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

  it('keeps a hostile category/trait label and evidence value inert once expanded', async () => {
    const hostileProfile: MemoryProfile = {
      summary: null,
      explicit_info: [
        {
          category: '<img src=x onerror=alert(1)>',
          description: 'Desc',
          evidence: '<script>alert(2)</script>',
        },
      ],
      implicit_traits: [],
      updated_at: null,
    };
    render(<StructuredMemoryProfile profile={hostileProfile} t={t} />);

    expect(document.querySelector('img')).toBeNull();
    const trigger = screen.getByText('<img src=x onerror=alert(1)>');
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('button', { name: 'memory.profile.evidence' }));

    expect(screen.getByText('<script>alert(2)</script>')).toBeTruthy();
    expect(document.querySelector('script[src]')).toBeNull();
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

  it('resets a refreshed entry to closed even when the reloaded profile is byte-identical', async () => {
    const profileItems = [{ kind: 'profile' as const, text: '', date: null, profile: PROFILE }];
    // Two structurally distinct arrays with identical content: a real refetch
    // never hands back the same object identity, and content-derived entry
    // keys alone must not let React reuse the previous (opened) instance.
    getMemoryProfile.mockResolvedValue({ status: 'ok', items: [...profileItems], warnings: [] });

    render(<MemoryProfilePanel enabled />);
    const trigger = await screen.findByRole('button', { name: 'communication' });
    await userEvent.click(trigger);
    expect(screen.getByText('Prefers written updates.')).toBeTruthy();

    getMemoryProfile.mockResolvedValue({ status: 'ok', items: [...profileItems], warnings: [] });
    await userEvent.click(screen.getByRole('button', { name: 'memory.profile.refresh' }));

    const refreshedTrigger = await screen.findByRole('button', { name: 'communication' });
    expect(refreshedTrigger.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Prefers written updates.')).toBeNull();
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
