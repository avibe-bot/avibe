import type { RouteCollectionObservation, RouteReport } from "./RouteChainDialog";
import type { AgentSupply, Source } from "./types";

export type RouteProjectionMember = "agents" | "sources";

export type RouteProjectionStatus = {
  report: RouteReport;
  reports: readonly RouteReport[];
  failed: ReadonlySet<RouteProjectionMember>;
  pending: boolean;
};

type RouteProjectionReaders = {
  readAgents: () => Promise<RouteCollectionObservation<AgentSupply[]>>;
  readSources: () => Promise<RouteCollectionObservation<Source[]>>;
  onFailure: (member: RouteProjectionMember) => void;
  onStatus: (status: RouteProjectionStatus) => void;
};

/** One page-owned collection reader, with exact evidence for every outstanding
 * commit. Failed reports do not block a later commit's newer collection reads. */
export const createRouteProjectionReconciler = ({
  readAgents,
  readSources,
  onFailure,
  onStatus,
}: RouteProjectionReaders) => {
  let generation = 0;
  let pending = false;
  const failed = new Set<RouteProjectionMember>();
  let reports: RouteReport[] = [];
  const queuedReports: RouteReport[] = [];

  const publish = () => {
    const report = reports[reports.length - 1];
    if (report) onStatus({ report, reports: [...reports], pending, failed: new Set(failed) });
  };

  const settle = async (members: ReadonlySet<RouteProjectionMember>) => {
    const token = ++generation;
    pending = true;
    publish();
    try {
      if (members.has("agents")) {
        try {
          const observation = await readAgents();
          if (token !== generation) return;
          observation.install();
          failed.delete("agents");
        } catch {
          if (token !== generation) return;
          failed.add("agents");
          onFailure("agents");
          return;
        }
      }
      if (members.has("sources") || members.has("agents")) {
        try {
          const observation = await readSources();
          if (token !== generation) return;
          observation.install();
          failed.delete("sources");
        } catch {
          if (token !== generation) return;
          failed.add("sources");
          onFailure("sources");
        }
      }
    } finally {
      if (token === generation) {
        pending = false;
        publish();
        if (failed.size === 0) reports = [];
        beginNext();
      }
    }
  };

  const beginNext = () => {
    if (pending || queuedReports.length === 0) return;
    // All reads in this batch begin after every included commit. A newer full
    // collection can satisfy older failed obligations without losing evidence.
    reports.push(...queuedReports.splice(0));
    void settle(new Set(["agents"]));
  };

  return {
    start: (committed: RouteReport) => {
      queuedReports.push(committed);
      if (pending) publish();
      else beginNext();
    },
    retry: () => {
      if (pending || failed.size === 0) return;
      void settle(new Set(failed));
    },
    invalidate: () => {
      generation += 1;
      pending = false;
      reports = [];
      queuedReports.length = 0;
      failed.clear();
    },
  };
};
