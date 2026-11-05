/* @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Skeleton, SkeletonShimmer } from './skeleton';
import { HANDOVER_CLASS, useSkeletonHandover } from './skeleton-handover';

// The shared skeleton (ui/design: system/skeleton/*). What a test can hold it to is
// its classes: the motion lives in CSS, and jsdom runs none of it.

afterEach(cleanup);

describe('SkeletonShimmer', () => {
  it('announces one busy region, holds its space before it shows, and sweeps one light highlight', () => {
    render(
      <SkeletonShimmer label="Loading">
        <Skeleton className="h-2 w-10" />
      </SkeletonShimmer>,
    );
    const region = screen.getByRole('status');
    expect(region.getAttribute('aria-busy')).toBe('true');
    expect(screen.getByText('Loading').className).toContain('sr-only');
    // Shown only after the reveal delay, so a fast read never flashes it.
    expect(region.className).toContain('animate-skeleton-reveal');
    const highlight = region.querySelector('.skeleton-shimmer')!;
    expect(highlight.className).toContain('animate-skeleton-shimmer');
    // The light strength the owner chose (2026-11-05).
    expect(highlight.className).toContain('via-foreground/[0.03]');
  });

  it('rests the highlight off the card through the property its sweep animates', () => {
    // A translate-x utility sets CSS `translate`, which composes with the sweep's `transform`
    // instead of being replaced by it: the highlight would run from -200% to 0% and sit on
    // the card for the rest of each period. One property: rest and sweep stay off the card.
    const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8');
    const keyframes = /@keyframes skeleton-shimmer\s*\{([\s\S]*?)\n\s*\}/.exec(css)?.[1] ?? '';
    const animated = new Set([...keyframes.matchAll(/\{\s*([a-z-]+)\s*:/g)].map((match) => match[1]));
    expect([...animated]).toEqual(['transform']);
    render(<SkeletonShimmer><Skeleton /></SkeletonShimmer>);
    const highlight = screen.getByRole('status').querySelector('.skeleton-shimmer')!;
    expect(highlight.className).toContain('[transform:translateX(-100%)]');
    expect(highlight.className).not.toMatch(/(^|\s)-?translate-/);
  });

  it('stands still under reduced motion: no sweep and no reveal fade', () => {
    render(<SkeletonShimmer><Skeleton /></SkeletonShimmer>);
    const region = screen.getByRole('status');
    expect(region.className).toContain('motion-reduce:animate-none');
    expect(region.querySelector('.skeleton-shimmer')!.className).toContain('motion-reduce:animate-none');
  });
});

describe('useSkeletonHandover', () => {
  function Place({ skeleton }: { skeleton: boolean }) {
    const handover = useSkeletonHandover(skeleton);
    return skeleton ? <p>skeleton</p> : <p data-testid="content" className={handover}>content</p>;
  }

  it('fades in content that took a skeleton\'s place', () => {
    const view = render(<Place skeleton />);
    view.rerender(<Place skeleton={false} />);
    expect(screen.getByTestId('content').className).toBe(HANDOVER_CLASS);
  });

  it('leaves content that was there from the start (a cached copy) to appear at once', () => {
    render(<Place skeleton={false} />);
    expect(screen.getByTestId('content').className).toBe('');
  });
});
