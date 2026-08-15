import * as React from 'react';

import type {
  PresentSourceMutationCommit,
  SourceMutationCommit,
  SourceMutationLanding,
} from './mutationSettlement';

export type SourceMutationReportState = {
  commit: SourceMutationCommit;
  landingFailed: boolean;
  busy: boolean;
};

type HeldSourceMutationReport = {
  state: SourceMutationReportState;
  resolve: () => void;
};

const settleCommit = async (
  commit: SourceMutationCommit,
): Promise<SourceMutationLanding | null> => {
  try {
    return await commit.settle();
  } catch {
    return null;
  }
};

/** Page-owned post-commit evidence; a Source panel may disappear without releasing it. */
export const useSourceMutationReport = () => {
  const [report, setReport] = React.useState<SourceMutationReportState | null>(null);
  const queueRef = React.useRef<HeldSourceMutationReport[]>([]);
  const mountedRef = React.useRef(true);

  const resolveHeldReports = React.useCallback(() => {
    const held = queueRef.current;
    queueRef.current = [];
    for (const entry of held) entry.resolve();
  }, []);

  const releaseCurrent = React.useCallback(() => {
    const [current, ...remaining] = queueRef.current;
    if (!current) return;
    queueRef.current = remaining;
    current.resolve();
    setReport(remaining[0]?.state ?? null);
  }, []);

  const hold = React.useCallback((
    commit: SourceMutationCommit,
    landingFailed: boolean,
  ): Promise<void> => {
    if (!mountedRef.current) return Promise.resolve();
    return new Promise((resolve) => {
      const held = { state: { commit, landingFailed, busy: false }, resolve };
      queueRef.current.push(held);
      if (queueRef.current.length === 1) setReport(held.state);
    });
  }, []);

  const present = React.useCallback<PresentSourceMutationCommit>(async (commit) => {
    if (commit.impact) {
      await hold(commit, false);
      return;
    }
    const landing = await settleCommit(commit);
    if (landing?.verdict === 'landed') return;
    await hold(commit, true);
  }, [hold]);

  const complete = React.useCallback(async () => {
    const current = queueRef.current[0];
    if (!current || current.state.busy) return;
    current.state = { ...current.state, busy: true };
    setReport(current.state);
    const landing = await settleCommit(current.state.commit);
    if (!mountedRef.current || queueRef.current[0] !== current) return;
    if (landing?.verdict === 'landed') {
      releaseCurrent();
      return;
    }
    current.state = { ...current.state, landingFailed: true, busy: false };
    setReport(current.state);
  }, [releaseCurrent]);

  React.useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      resolveHeldReports();
    };
  }, [resolveHeldReports]);

  return { report, present, complete, dismiss: releaseCurrent };
};
