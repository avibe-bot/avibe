import * as React from 'react';
import { modelsApi } from './modelsApi';
import type { AgentBackend, RouteHop, TurnProvenance } from './types';

export type RecordedError = TurnProvenance & { terminal_error: NonNullable<TurnProvenance['terminal_error']> };
export type RecordedTurn = { record: RecordedError | null; failed: boolean; retry: () => void };

/** The latest recorded turn for this exact model, kept only when it ended in an error. */
export function useRecordedTurn(backend: AgentBackend | undefined, modelId: string): RecordedTurn {
  const [record, setRecord] = React.useState<TurnProvenance | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [attempt, setAttempt] = React.useState(0);
  React.useEffect(() => {
    let active = true;
    setRecord(null);
    setFailed(false);
    if (!backend) return;
    void modelsApi.getAgentProvenance(backend, modelId).then((value) => {
      if (active) setRecord(value);
    }, () => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [backend, modelId, attempt]);
  const retry = React.useCallback(() => setAttempt((value) => value + 1), []);
  const shown = record?.terminal_error && record.agent === backend && record.requested_model_id === modelId
    ? record as RecordedError : null;
  return { record: shown, failed, retry };
}

/** The recorded error names the hop that failed; it is drawn on that hop when the route still has it. */
export const recordedOn = (record: RecordedError | null, hop: RouteHop): boolean =>
  record !== null && record.terminal_error.source_id === hop.source_id
  && record.terminal_error.configured_model_id === hop.model_id;
