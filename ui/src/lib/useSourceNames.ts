import { useEffect, useState } from 'react';

import { modelsApi } from '../components/settings/models/modelsApi';

/**
 * Model Hub Source display names by id, read once per activation of the surface
 * that shows them, when it first wants them.
 *
 * Sources are renamed, added and replaced in Settings, which covers the surface,
 * so each uncovering reads them afresh; the last names show until the new answer
 * lands. Within one activation the names are read once, not on every open: the
 * Sources read carries each Source's whole model list. The read is a Member rule,
 * and some surfaces that name a Source (the Chat route picker) are Editor
 * surfaces, so a refused read is an expected answer: it yields null, and the
 * caller says what it can without the names.
 */
export function useSourceNames(wanted: boolean, surfaceActive: boolean): Record<string, string> | null {
  const [names, setNames] = useState<Record<string, string> | null>(null);
  // Whether this activation has its answer.
  const [answered, setAnswered] = useState(false);
  useEffect(() => {
    if (!surfaceActive) setAnswered(false);
  }, [surfaceActive]);
  useEffect(() => {
    if (!wanted || !surfaceActive || answered) return undefined;
    let current = true;
    modelsApi.listSources().then(
      (sources) => {
        if (!current) return;
        setNames(Object.fromEntries(sources.map((source) => [source.id, source.display_name])));
        setAnswered(true);
      },
      () => {
        // Refused (a role that may not read Sources) or unreachable: the hint is
        // generic, never names kept from an earlier read.
        if (!current) return;
        setNames(null);
        setAnswered(true);
      },
    );
    return () => { current = false; };
  }, [wanted, surfaceActive, answered]);
  return names;
}
