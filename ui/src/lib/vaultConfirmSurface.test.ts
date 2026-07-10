import { describe, expect, it } from 'vitest';

import { buildVaultConfirmSurface, type VaultConfirmSurfaceMeasurement } from './vaultConfirmSurface';

// A fully-visible modal measurement (440×640 centered iframe, opaque, interactive).
const visible: VaultConfirmSurfaceMeasurement = {
  frameWidth: 440,
  frameHeight: 640,
  intersectionRatio: 1,
  visibleByIntersectionObserver: true,
  opacity: '1',
  pointerEvents: 'auto',
  sampledAt: 1_700_000_000_000,
};

describe('buildVaultConfirmSurface (protocol v2 §6.6 / §13 wire shape)', () => {
  it('nests every field under `frame` with a top-level `sampledAt`, matching parseParentConfirmSurface', () => {
    const surface = buildVaultConfirmSurface(visible);
    // Freeze the exact keys the sandbox parser reads. A rename here is the cross-repo contract-shape
    // gap that surfaced as "parent frame visibility is not attested".
    expect(Object.keys(surface).sort()).toEqual(['frame', 'sampledAt']);
    expect(Object.keys(surface.frame).sort()).toEqual([
      'frameHeight',
      'frameWidth',
      'intersectionRatio',
      'opacity',
      'pointerEvents',
      'visibleByIntersectionObserver',
    ]);
    expect(surface).toEqual({
      frame: {
        frameWidth: 440,
        frameHeight: 640,
        intersectionRatio: 1,
        visibleByIntersectionObserver: true,
        opacity: 1,
        pointerEvents: true,
      },
      sampledAt: 1_700_000_000_000,
    });
  });

  it('parses a computed opacity string and fails closed (0) on a non-numeric opacity', () => {
    expect(buildVaultConfirmSurface({ ...visible, opacity: '0.5' }).frame.opacity).toBe(0.5);
    expect(buildVaultConfirmSurface({ ...visible, opacity: 0.25 }).frame.opacity).toBe(0.25);
    expect(buildVaultConfirmSurface({ ...visible, opacity: '' }).frame.opacity).toBe(0);
    expect(buildVaultConfirmSurface({ ...visible, opacity: 'inherit' }).frame.opacity).toBe(0);
  });

  it('coerces computed pointer-events honestly: only "none" is non-interactive', () => {
    expect(buildVaultConfirmSurface({ ...visible, pointerEvents: 'none' }).frame.pointerEvents).toBe(false);
    expect(buildVaultConfirmSurface({ ...visible, pointerEvents: 'auto' }).frame.pointerEvents).toBe(true);
    expect(buildVaultConfirmSurface({ ...visible, pointerEvents: false }).frame.pointerEvents).toBe(false);
  });

  it('passes a degenerate (headless/hidden) measurement through unfaked so the sandbox gate rejects it', () => {
    const headless = buildVaultConfirmSurface({
      frameWidth: 0,
      frameHeight: 0,
      intersectionRatio: 0,
      visibleByIntersectionObserver: false,
      opacity: '1',
      pointerEvents: 'none',
      sampledAt: 1_700_000_000_000,
    });
    expect(headless.frame.frameWidth).toBe(0);
    expect(headless.frame.frameHeight).toBe(0);
    expect(headless.frame.intersectionRatio).toBe(0);
    expect(headless.frame.visibleByIntersectionObserver).toBe(false);
    expect(headless.frame.pointerEvents).toBe(false);
  });
});
