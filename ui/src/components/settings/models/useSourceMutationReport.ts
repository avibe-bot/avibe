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
  const releaseRef = React.useRef<(() => void) | null>(null);
  const mountedRef = React.useRef(true);

  const resolveHeldReport = React.useCallback(() => {
    const resolve = releaseRef.current;
    releaseRef.current = null;
    resolve?.();
  }, []);

  const release = React.useCallback(() => {
    setReport(null);
    resolveHeldReport();
  }, [resolveHeldReport]);

  const hold = React.useCallback((
    commit: SourceMutationCommit,
    landingFailed: boolean,
  ): Promise<void> => {
    if (!mountedRef.current) return Promise.resolve();
    return new Promise((resolve) => {
      releaseRef.current = resolve;
      setReport({ commit, landingFailed, busy: false });
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
    if (!report || report.busy) return;
    setReport({ ...report, busy: true });
    const landing = await settleCommit(report.commit);
    if (!mountedRef.current) return;
    if (landing?.verdict === 'landed') {
      release();
      return;
    }
    setReport({ ...report, landingFailed: true, busy: false });
  }, [release, report]);

  React.useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      resolveHeldReport();
    };
  }, [resolveHeldReport]);

  return { report, present, complete, dismiss: release };
};
