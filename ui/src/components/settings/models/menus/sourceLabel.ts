// Compact source label used in the mapping candidate line ("智谱 / relay.example",
// frame 04): the vendor's friendly name for known vendors, else the source
// display name (host for a custom endpoint). Kept in its own module so
// `supplyBits.tsx` stays component-only (react-refresh).
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import type { Source } from '../types';

/** Returns a stable function mapping a source to a compact label. */
export function useCompactSourceLabel(): (source: Source) => string {
  const { t } = useTranslation();
  return React.useCallback(
    (source: Source) => {
      if (source.vendor && source.vendor !== 'custom') {
        const label = t(`settings.models.addKey.vendors.${source.vendor}`, { defaultValue: '' }) as string;
        if (label) return label;
      }
      return source.display_name;
    },
    [t],
  );
}
