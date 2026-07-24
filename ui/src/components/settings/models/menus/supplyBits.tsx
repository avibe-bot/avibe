// The supply dots — one colored dot per supplying source (frames 04 / 05r).
// (The compact source label hook lives in ./sourceLabel to keep this file
// component-only for fast refresh.)
import * as React from 'react';

import { Dot } from '../chips';
import type { Accent } from '../vendorMeta';

/** A run of supply dots — colored per supplying source. */
export const SupplyDots: React.FC<{ accents: Accent[]; className?: string }> = ({ accents, className }) => (
  <span className={className}>
    {accents.map((accent, i) => (
      <Dot key={`${accent}-${i}`} accent={accent} />
    ))}
  </span>
);
