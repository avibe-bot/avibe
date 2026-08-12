import type { RouteCollectionObservation } from "./RouteChainDialog";
import type { AgentSupply, Source } from "./types";
import type { AgentBackend } from "./types";

export type RouteProjectionMember = "agents" | "sources";

export type RouteProjectionStatus = {
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
  let activeBackend: AgentBackend | null = null;

  const publish = (pending: boolean) =>
    onStatus({ pending, failed: new Set(failed) });

  const settle = async (
    token: number,
    backend: AgentBackend,
    members: ReadonlySet<RouteProjectionMember>,
  ) => {
    failed = new Set();
    publish(true);

    if (members.has("agents")) {
      try {
        const observation = await readAgents();
        if (token !== generation) return;
        if (!observation.value.some((agent) => agent.backend === backend)) {
          throw new Error("route_agent_missing");
        }
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
    start: (backend: AgentBackend) => {
      activeBackend = backend;
      const token = ++generation;
      void settle(token, backend, new Set(["agents"]));
    },
    retry: () => {
      if (failed.size === 0 || !activeBackend) return;
      const token = ++generation;
      void settle(token, activeBackend, failed);
    },
    invalidate: () => {
      generation += 1;
    },
  };
};
