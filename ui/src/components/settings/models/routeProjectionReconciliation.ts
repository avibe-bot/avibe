import type { RouteCollectionObservation, RouteReport } from "./RouteChainDialog";
import type { AgentSupply, Source } from "./types";

export type RouteProjectionMember = "agents" | "sources";

export type RouteProjectionStatus = {
  report: RouteReport;
  failed: ReadonlySet<RouteProjectionMember>;
  pending: boolean;
};

type RouteProjectionReaders = {
  readAgents: () => Promise<RouteCollectionObservation<AgentSupply[]>>;
  readSources: () => Promise<RouteCollectionObservation<Source[]>>;
  onFailure: (member: RouteProjectionMember) => void;
  onStatus: (status: RouteProjectionStatus) => void;
};

/** M6 is page-owned: closing the modal changes presentation ownership but never
 * cancels, restarts or broadens the projection generation. */
export const createRouteProjectionReconciler = ({
  readAgents,
  readSources,
  onFailure,
  onStatus,
}: RouteProjectionReaders) => {
  let generation = 0;
  let failed = new Set<RouteProjectionMember>();
  let activeReport: RouteReport | null = null;
  const pendingReports: RouteReport[] = [];

  const publish = (pending: boolean) => {
    if (activeReport) {
      onStatus({ report: activeReport, pending, failed: new Set(failed) });
    }
  };

  const settle = async (
    token: number,
    members: ReadonlySet<RouteProjectionMember>,
  ) => {
    failed = new Set();
    publish(true);

    if (members.has("agents")) {
      try {
        const observation = await readAgents();
        if (token !== generation) return;
        observation.install();
      } catch {
        if (token !== generation) return;
        failed.add("agents");
        onFailure("agents");
        publish(false);
        return;
      }
    }

    if (members.has("sources") || members.has("agents")) {
      try {
        const observation = await readSources();
        if (token !== generation) return;
        observation.install();
      } catch {
        if (token !== generation) return;
        failed.add("sources");
        onFailure("sources");
      }
    }
    if (token === generation) publish(false);
  };

  const beginNext = () => {
    if (activeReport || pendingReports.length === 0) return;
    activeReport = pendingReports.shift() ?? null;
    if (!activeReport) return;
    const token = ++generation;
    void settle(token, new Set(["agents"])).then(() => {
      if (token !== generation || failed.size > 0) return;
      activeReport = null;
      beginNext();
    });
  };

  return {
    start: (committed: RouteReport) => {
      // The page owns each complete received/inferred response before the editor
      // closes. Queue later commits so an in-flight reconciliation never drops
      // an earlier report or cancels its projection settlement.
      pendingReports.push(committed);
      beginNext();
    },
    retry: () => {
      if (failed.size === 0 || !activeReport) return;
      const token = ++generation;
      void settle(token, failed).then(() => {
        if (token !== generation || failed.size > 0) return;
        activeReport = null;
        beginNext();
      });
    },
    invalidate: () => {
      generation += 1;
    },
  };
};
