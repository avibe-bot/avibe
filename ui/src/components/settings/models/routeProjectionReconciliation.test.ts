import { describe, expect, it, vi } from "vitest";

import { createRouteProjectionReconciler } from "./routeProjectionReconciliation";
import type { RouteReport } from "./RouteChainDialog";
import type { AgentSupply, Source } from "./types";

const agent: AgentSupply = {
  backend: "claude",
  cli_present: true,
  menu_kind: "fixed",
  mode: "hub",
};
const source: Source = {
  id: "source",
  last_discovered_at: null,
  kind: "api_key",
  vendor: "anthropic",
  display_name: "Source",
  protocol: "anthropic",
  supply_channel: "hub",
  billing: "metered",
  state: { status: "active", retry_at: null, detail_key: null },
  models: [],
};
const report: RouteReport = {
  chain: {
    contract_version: 10,
    backend: "claude",
    model_id: "模型/opus",
    manual_override: null,
    route_origin: null,
    chain: [],
    current: null,
    supply_state: "interrupted",
  },
  removed_hops: [],
  interrupted: [],
};

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
};

describe("route projection reconciliation", () => {
  it("installs Agents before acquiring Sources and survives presentation transfer", async () => {
    const agents = deferred<{ value: AgentSupply[]; install: () => void }>();
    const agentInstall = vi.fn();
    const sourceInstall = vi.fn();
    const readSources = vi.fn().mockResolvedValue({
      value: [source],
      install: sourceInstall,
    });
    const statuses = vi.fn();
    const reconciler = createRouteProjectionReconciler({
      readAgents: vi.fn().mockReturnValue(agents.promise),
      readSources,
      onFailure: vi.fn(),
      onStatus: statuses,
    });

    reconciler.start(report);
    expect(readSources).not.toHaveBeenCalled();
    agents.resolve({ value: [agent], install: agentInstall });
    await vi.waitFor(() => expect(sourceInstall).toHaveBeenCalledTimes(1));
    expect(agentInstall).toHaveBeenCalledTimes(1);
    expect(readSources).toHaveBeenCalledTimes(1);
    expect(statuses).toHaveBeenLastCalledWith({
      report,
      pending: false,
      failed: new Set(),
    });
  });

  it("installs an authoritative collection that omits the edited backend", async () => {
    const agentInstall = vi.fn();
    const sourceInstall = vi.fn();
    const readSources = vi.fn().mockResolvedValue({
      value: [source],
      install: sourceInstall,
    });
    const statuses = vi.fn();
    const reconciler = createRouteProjectionReconciler({
      readAgents: vi.fn().mockResolvedValue({ value: [], install: agentInstall }),
      readSources,
      onFailure: vi.fn(),
      onStatus: statuses,
    });

    reconciler.start(report);

    await vi.waitFor(() => expect(sourceInstall).toHaveBeenCalledTimes(1));
    expect(agentInstall).toHaveBeenCalledTimes(1);
    expect(readSources).toHaveBeenCalledTimes(1);
    expect(statuses).toHaveBeenLastCalledWith({
      report,
      pending: false,
      failed: new Set(),
    });
  });

  it("retries an Agents failure before activating its deferred Source member", async () => {
    const readAgents = vi
      .fn()
      .mockRejectedValueOnce(new Error("unread"))
      .mockResolvedValueOnce({ value: [agent], install: vi.fn() });
    const readSources = vi
      .fn()
      .mockResolvedValue({ value: [source], install: vi.fn() });
    const statuses = vi.fn();
    const reconciler = createRouteProjectionReconciler({
      readAgents,
      readSources,
      onFailure: vi.fn(),
      onStatus: statuses,
    });

    reconciler.start(report);
    await vi.waitFor(() =>
      expect(statuses).toHaveBeenLastCalledWith({
        report,
        pending: false,
        failed: new Set(["agents"]),
      }),
    );
    expect(readSources).not.toHaveBeenCalled();
    reconciler.retry();
    await vi.waitFor(() => expect(readSources).toHaveBeenCalledTimes(1));
    expect(readAgents).toHaveBeenCalledTimes(2);
  });

  it("retries only Sources after Agents has already settled", async () => {
    const readAgents = vi
      .fn()
      .mockResolvedValue({ value: [agent], install: vi.fn() });
    const readSources = vi
      .fn()
      .mockRejectedValueOnce(new Error("unread"))
      .mockResolvedValueOnce({ value: [source], install: vi.fn() });
    const statuses = vi.fn();
    const reconciler = createRouteProjectionReconciler({
      readAgents,
      readSources,
      onFailure: vi.fn(),
      onStatus: statuses,
    });

    reconciler.start(report);
    await vi.waitFor(() =>
      expect(statuses).toHaveBeenLastCalledWith({
        report,
        pending: false,
        failed: new Set(["sources"]),
      }),
    );
    reconciler.retry();
    await vi.waitFor(() => expect(readSources).toHaveBeenCalledTimes(2));
    expect(readAgents).toHaveBeenCalledTimes(1);
  });

  it.each(["nonempty", "empty", "unavailable"] as const)(
    "retains exact %s commit tails through pending, failure, Retry and settlement",
    async (tail) => {
      const committed: RouteReport = {
        chain: report.chain,
        removed_hops: tail === "unavailable" ? null : tail === "empty" ? [] : [{
          backend: "claude", menu_model: "模型/opus", position: 1,
          source_id: source.id, model_id: "模型/opus",
        }],
        interrupted: tail === "unavailable" ? null : tail === "empty" ? [] : [{
          backend: "claude", model_id: "模型/opus", agents: ["写作 Agent"],
        }],
      };
      const sources = deferred<{ value: Source[]; install: () => void }>();
      const statuses = vi.fn();
      const reconciler = createRouteProjectionReconciler({
        readAgents: vi.fn().mockResolvedValue({ value: [agent], install: vi.fn() }),
        readSources: vi.fn()
          .mockRejectedValueOnce(new Error("offline"))
          .mockReturnValueOnce(sources.promise),
        onFailure: vi.fn(),
        onStatus: statuses,
      });

      reconciler.start(committed);
      expect(statuses).toHaveBeenLastCalledWith({
        report: committed, pending: true, failed: new Set(),
      });
      await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
        report: committed, pending: false, failed: new Set(["sources"]),
      }));
      reconciler.retry();
      expect(statuses).toHaveBeenLastCalledWith({
        report: committed, pending: true, failed: new Set(),
      });
      sources.resolve({ value: [source], install: vi.fn() });
      await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
        report: committed, pending: false, failed: new Set(),
      }));
      for (const [status] of statuses.mock.calls) expect(status.report).toBe(committed);
    },
  );
});
