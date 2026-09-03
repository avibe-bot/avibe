// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';

import i18n from '@/i18n';
import { GuardImpact } from './GuardImpact';
import type { RouteHopRef } from './types';

const hop = (overrides: Partial<RouteHopRef> = {}): RouteHopRef => ({
  backend: 'claude',
  menu_model: 'alpha',
  source_id: 'src_a',
  model_id: 'alpha-air',
  position: 1,
  ...overrides,
});

const renderImpact = (props: React.ComponentProps<typeof GuardImpact>) => render(
  <I18nextProvider i18n={i18n}>
    <GuardImpact {...props} />
  </I18nextProvider>,
);

/** What one hop row reads as, derived from the same map the caller passed — so
 *  the expectation is the rule rather than a copy of the output. */
const hopText = (target: RouteHopRef, names: Readonly<Record<string, string>>) => {
  const source = names[target.source_id];
  return `${source ? `${source} → ${target.model_id}` : target.model_id} · Order #${target.position}`;
};

afterEach(async () => {
  cleanup();
  await i18n.changeLanguage('en');
});

describe('GuardImpact', () => {
  it('names the supplier of every hop the caller can resolve, and leaves the rest unnamed', () => {
    // One hop of each shape the map can produce, so an id the page does not
    // cover is exercised by construction rather than by being remembered.
    const hops = [
      hop(),
      hop({ menu_model: 'beta', source_id: 'src_missing', model_id: 'beta-air', position: 2 }),
    ];
    const sourceNames = { src_a: 'Primary relay' };
    const { container } = renderImpact({ hops, gaps: [], sourceNames });

    expect(container.querySelectorAll('.model-hub-guard-hop')).toHaveLength(hops.length);
    for (const target of hops) {
      expect(within(container).getByText(hopText(target, sourceNames))).toBeTruthy();
    }
    // An id no Source answers for stays out of the copy entirely: a raw `src_…`
    // in front of a model would name nothing while looking like it did.
    expect(within(container).queryByText(/src_/)).toBeNull();
  });

  it('says nothing about a supplier when the caller names none', () => {
    const hops = [
      hop(),
      hop({ menu_model: 'beta', source_id: 'src_b', model_id: 'beta-air', position: 2 }),
    ];
    const omitted = renderImpact({ hops, gaps: [] });

    expect(omitted.container.querySelectorAll('.model-hub-guard-hop')).toHaveLength(hops.length);
    for (const target of hops) {
      expect(within(omitted.container).getByText(hopText(target, {}))).toBeTruthy();
    }

    // A map that resolves none of these hops renders identically to no map at
    // all, so the callers that pass nothing keep exactly the body they had and
    // a caller whose Sources simply do not cover a hop cannot look different.
    const empty = renderImpact({ hops, gaps: [], sourceNames: {} });
    expect(empty.container.innerHTML).toBe(omitted.container.innerHTML);
  });

  it('distinguishes a stated absence of gaps from nobody having stated one', () => {
    // Every claim this body can make about interruption, read off the keys
    // rather than copied, so a reworded claim is caught by the same assertion.
    const claims = [
      'settings.models.guard.hint.safe',
      'settings.models.guard.hint.interrupt',
      'settings.models.guard.result.hint.safe',
      'settings.models.guard.result.hint.interrupt',
      'settings.models.guard.gap.label',
      'settings.models.guard.result.gapLabel',
    ].map((key) => i18n.t(key));

    // `[]` is the guard saying so, and it is entitled to: it can see the supply.
    const stated = renderImpact({ hops: [hop()], gaps: [] });
    expect(stated.container.textContent).toContain(i18n.t('settings.models.guard.hint.safe'));

    // `null` is a caller that cannot see the supply and does not pretend to.
    const unstated = renderImpact({ hops: [hop()], gaps: null });

    // The hops survive — they are what such a caller does know — and every
    // statement about what removing them costs elsewhere is withheld.
    expect(within(unstated.container).getByText(hopText(hop(), {}))).toBeTruthy();
    for (const claim of claims) {
      expect(unstated.container.textContent).not.toContain(claim);
    }
  });

  it('keeps the mapping copy the rest of the Model Hub uses, in either language', async () => {
    await i18n.changeLanguage('zh');
    const sourceNames = { src_a: '主中继' };
    const { container } = renderImpact({ hops: [hop()], gaps: [], sourceNames });

    // The same key the Agent card renders a supply mapping with, so one mapping
    // reads one way wherever the Model Hub shows it — including where zh and en
    // deliberately share the arrow.
    expect(within(container).getByText(new RegExp(`${sourceNames.src_a} → alpha-air`))).toBeTruthy();
    expect(screen.getByText(i18n.t('settings.models.guard.label'))).toBeTruthy();
  });
});
