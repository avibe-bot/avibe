import { useEffect, useMemo, useState } from 'react';

import { modelsApi } from '../components/settings/models/modelsApi';

/**
 * Model Hub Source display names by id, read once per activation of the surface
 * that shows them and per viewer (`viewer` names the role reading them), when
 * it first wants them.
 *
 * Sources are renamed, added and replaced in Settings, which covers the surface,
 * so each uncovering reads them afresh; the last names show until the new answer
 * lands. Within one activation the names are read once, not on every open: the
 * Sources read carries each Source's whole model list. The read is a Member rule,
 * and some surfaces that name a Source (the Chat route picker) are Editor
 * surfaces, so a refused read is an expected answer: it yields null, and the
 * caller says what it can without the names.
 */
export function useSourceNames(wanted: boolean, surfaceActive: boolean, viewer: string): Record<string, string> | null {
  // The names and the viewer they were read for: they show to that viewer only,
  // so a role change hides them at once, before its own read answers.
  const [names, setNames] = useState<{ viewer: string; byId: Record<string, string> } | null>(null);
  // One token per activation and viewer: each change of `surfaceActive` starts a
  // new activation, and a change of who is looking (a role change) may change
  // whether the names may be read at all. The last answer belongs to one token.
  const activation = useMemo(() => ({ surfaceActive, viewer }), [surfaceActive, viewer]);
  const [answeredFor, setAnsweredFor] = useState<object | null>(null);
  useEffect(() => {
    if (!wanted || !surfaceActive || answeredFor === activation) return undefined;
    let current = true;
    modelsApi.listSources().then(
      (sources) => {
        if (!current) return;
        setNames({ viewer, byId: Object.fromEntries(sources.map((source) => [source.id, source.display_name])) });
        setAnsweredFor(activation);
      },
      () => {
        // Refused (a role that may not read Sources) or unreachable: the hint is
        // generic, never names kept from an earlier read.
        if (!current) return;
        setNames(null);
        setAnsweredFor(activation);
      },
    );
    return () => { current = false; };
  }, [wanted, surfaceActive, answeredFor, activation, viewer]);
  return names !== null && names.viewer === viewer ? names.byId : null;
}
