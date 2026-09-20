import { readRegion, type RegionRead } from './regionRead';
import type { AgentSupply, RuntimeDependency, Source } from './types';

/**
 * The operational overview cannot wait for ancillary history or per-model
 * projections. Every member names why it owns a place in the first-paint
 * barrier, so adding another member requires an explicit policy decision.
 */
type FirstPaintRegionValues = {
  sources: Source[];
  supply: AgentSupply[];
  runtime: RuntimeDependency;
};

export const FIRST_PAINT_REGION_WHITELIST = {
  sources: 'draws the source inventory',
  supply: 'draws backend supply groups',
  runtime: 'determines runtime availability',
} as const satisfies Record<keyof FirstPaintRegionValues, string>;

export type FirstPaintRegionReaders = {
  [K in keyof FirstPaintRegionValues]: () => Promise<FirstPaintRegionValues[K]>;
};

export type SurfaceLanding = {
  [K in keyof FirstPaintRegionValues]: RegionRead<FirstPaintRegionValues[K]>;
};

export const readFirstPaintRegions = async (
  readers: FirstPaintRegionReaders,
): Promise<SurfaceLanding> => {
  const keys = Object.keys(FIRST_PAINT_REGION_WHITELIST) as (keyof FirstPaintRegionReaders)[];
  const entries = await Promise.all(keys.map(async (key) => {
    const reader = readers[key] as () => Promise<unknown>;
    return [key, await readRegion(reader)] as const;
  }));
  return Object.fromEntries(entries) as SurfaceLanding;
};
