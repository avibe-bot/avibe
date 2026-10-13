import { useEffect, useState } from 'react';

import { modelsApi } from '../components/settings/models/modelsApi';

/**
 * Model Hub Source display names by id, read once while `active`.
 *
 * The Sources read is a Member rule, and some surfaces that name a Source (the
 * Chat route picker) are Editor surfaces, so a refused read is an expected
 * answer: it yields null, and the caller says what it can without the names.
 */
export function useSourceNames(active: boolean): Record<string, string> | null {
  const [names, setNames] = useState<Record<string, string> | null>(null);
  useEffect(() => {
    if (!active || names !== null) return undefined;
    let current = true;
    modelsApi.listSources().then(
      (sources) => {
        if (current) setNames(Object.fromEntries(sources.map((source) => [source.id, source.display_name])));
      },
      () => {
        // Refused or unreachable: the hint stays generic.
      },
    );
    return () => { current = false; };
  }, [active, names]);
  return names;
}
