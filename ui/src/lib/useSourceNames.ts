import { useEffect, useState } from 'react';

import { modelsApi } from '../components/settings/models/modelsApi';

/**
 * Model Hub Source display names by id, read again each time `active` turns on.
 *
 * A Source can be renamed, added or replaced in Settings while a surface that
 * names it stays mounted, so a later activation reads the names afresh; the last
 * answer shows until the new one lands, and a failed read drops it. The Sources read is a Member rule, and
 * some surfaces that name a Source (the Chat route picker) are Editor surfaces,
 * so a refused read is an expected answer: it yields null, and the caller says
 * what it can without the names.
 */
export function useSourceNames(active: boolean): Record<string, string> | null {
  const [names, setNames] = useState<Record<string, string> | null>(null);
  useEffect(() => {
    if (!active) return undefined;
    let current = true;
    modelsApi.listSources().then(
      (sources) => {
        if (current) setNames(Object.fromEntries(sources.map((source) => [source.id, source.display_name])));
      },
      () => {
        // Refused (a role that may not read Sources) or unreachable: the hint is
        // generic, never names kept from an earlier read.
        if (current) setNames(null);
      },
    );
    return () => { current = false; };
  }, [active]);
  return names;
}
