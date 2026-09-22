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
  let report: RouteReport | null = null;

  const publish = (pending: boolean) => {
    if (report) onStatus({ report, pending, failed: new Set(failed) });
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

  return {
    start: (committed: RouteReport) => {
      // The page owns the complete received/inferred evidence before the editor
      // closes. Retry changes only projection status, never the response tails.
      report = committed;
      const token = ++generation;
      void settle(token, new Set(["agents"]));
    },
    retry: () => {
      if (failed.size === 0 || !report) return;
      const token = ++generation;
      void settle(token, failed);
    },
    invalidate: () => {
      generation += 1;
    },
  };
};
