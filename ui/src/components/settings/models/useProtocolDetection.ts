// Which API format a custom endpoint speaks, asked of that endpoint.
//
// Save-first stays the rule (docs/plans/model-hub-save-first-testing.md): this
// never gates the form. Once Base URL and key are filled it asks the existing,
// non-persisting observation route — the one the 09-03 detect-then-confirm
// design built — which probes the three formats against the typed address with
// the typed key and nothing else. The host decides what a recognised format does
// to its draft; this only reports one.
import * as React from 'react';

import { modelsApi } from './modelsApi';
import type { SourceObservation, SourceProtocol } from './types';

export type ProtocolDetection =
  | { kind: 'idle' }
  | { kind: 'detecting' }
  | { kind: 'detected'; protocol: SourceProtocol }
  /** Nothing proved a format. `rejected` when the endpoint refused the key. */
  | { kind: 'unrecognised'; rejected: boolean };

/** Long enough that a key being typed or pasted in parts is not probed per keystroke. */
export const DETECTION_DEBOUNCE_MS = 700;

const IDLE: ProtocolDetection = { kind: 'idle' };
const DETECTING: ProtocolDetection = { kind: 'detecting' };

const verdict = (observation: SourceObservation): ProtocolDetection => (
  observation.protocol && observation.protocol !== 'google'
    ? { kind: 'detected', protocol: observation.protocol }
    : { kind: 'unrecognised', rejected: observation.authenticated === 'rejected' }
);

/**
 * @param endpoint the address to probe, or `null` while there is none to probe.
 * @param paused holds an unanswered probe back without forgetting a settled
 *   answer — a save in flight is not a reason to probe, nor to ask again after.
 */
export function useProtocolDetection(
  endpoint: string | null,
  apiKey: string,
  paused: boolean,
): ProtocolDetection {
  const key = apiKey.trim();
  const target = endpoint ? `${endpoint}\n${key}` : null;
  const [settled, setSettled] = React.useState<{ target: string; result: ProtocolDetection } | null>(null);
  const answered = settled !== null && settled.target === target;

  React.useEffect(() => {
    if (!endpoint || target === null || paused || answered) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      modelsApi.observeApiKeySource({ vendor: 'custom', base_url: endpoint, key }, controller.signal)
        .then(verdict, () => ({ kind: 'unrecognised', rejected: false }) as const)
        .then((result) => {
          if (!controller.signal.aborted) setSettled({ target, result });
        });
    }, DETECTION_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [answered, endpoint, key, paused, target]);

  if (target === null || (paused && !answered)) return IDLE;
  return answered ? settled.result : DETECTING;
}
