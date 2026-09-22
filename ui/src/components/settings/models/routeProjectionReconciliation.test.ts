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
      reports: [report],
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
      reports: [report],
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
        reports: [report],
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
        reports: [report],
        pending: false,
        failed: new Set(["sources"]),
      }),
    );
    reconciler.retry();
    await vi.waitFor(() => expect(readSources).toHaveBeenCalledTimes(2));
    expect(readAgents).toHaveBeenCalledTimes(1);
  });

  it("queues a later commit until the earlier report settles", async () => {
    const firstAgents = deferred<{ value: AgentSupply[]; install: () => void }>();
    const firstSources = deferred<{ value: Source[]; install: () => void }>();
    const secondAgents = deferred<{ value: AgentSupply[]; install: () => void }>();
    const secondSources = deferred<{ value: Source[]; install: () => void }>();
    const firstReport = { ...report, chain: { ...report.chain, model_id: "first" } };
    const secondReport = { ...report, chain: { ...report.chain, model_id: "second" } };
    const statuses = vi.fn();
    const readAgents = vi.fn()
      .mockReturnValueOnce(firstAgents.promise)
      .mockReturnValueOnce(secondAgents.promise);
    const readSources = vi.fn()
      .mockReturnValueOnce(firstSources.promise)
      .mockReturnValueOnce(secondSources.promise);
    const reconciler = createRouteProjectionReconciler({
      readAgents,
      readSources,
      onFailure: vi.fn(),
      onStatus: statuses,
    });

    reconciler.start(firstReport);
    reconciler.start(secondReport);
    expect(readAgents).toHaveBeenCalledTimes(1);
    expect(statuses).toHaveBeenLastCalledWith({
      report: firstReport,
      reports: [firstReport],
      pending: true,
      failed: new Set(),
    });

    firstAgents.resolve({ value: [agent], install: vi.fn() });
    await vi.waitFor(() => expect(readSources).toHaveBeenCalledTimes(1));
    expect(statuses).toHaveBeenLastCalledWith({
      report: firstReport,
      reports: [firstReport],
      pending: true,
      failed: new Set(),
    });

    firstSources.resolve({ value: [source], install: vi.fn() });
    await vi.waitFor(() => expect(readAgents).toHaveBeenCalledTimes(2));
    expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport,
      reports: [secondReport],
      pending: true,
      failed: new Set(),
    });

    secondAgents.resolve({ value: [agent], install: vi.fn() });
    await vi.waitFor(() => expect(readSources).toHaveBeenCalledTimes(2));
    secondSources.resolve({ value: [source], install: vi.fn() });
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport,
      reports: [secondReport],
      pending: false,
      failed: new Set(),
    }));
    expect(statuses.mock.calls.map(([status]) => status.report)).toEqual([
      firstReport,
      firstReport,
      firstReport,
      secondReport,
      secondReport,
    ]);
  });

  it.each(["agents", "sources"] as const)(
    "settles an older %s failure with the next commit's newer collection reads",
    async (member) => {
    const secondAgents = deferred<{ value: AgentSupply[]; install: () => void }>();
    const firstReport: RouteReport = {
      ...report,
      removed_hops: [{ backend: "claude", menu_model: "模型/opus", position: 1, source_id: "source", model_id: "模型/opus" }],
    };
    const secondReport: RouteReport = { ...report, chain: { ...report.chain, backend: "codex" } };
    const statuses = vi.fn();
    const readAgents = vi.fn().mockResolvedValue({ value: [agent], install: vi.fn() });
    const readSources = vi.fn().mockResolvedValue({ value: [source], install: vi.fn() });
    (member === "agents" ? readAgents : readSources).mockRejectedValueOnce(new Error("offline"));
    const reconciler = createRouteProjectionReconciler({
      readAgents,
      readSources,
      onFailure: vi.fn(),
      onStatus: statuses,
    });

    reconciler.start(firstReport);
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report: firstReport,
      reports: [firstReport],
      pending: false,
      failed: new Set([member]),
    }));

    readAgents.mockReturnValueOnce(secondAgents.promise);
    reconciler.start(secondReport);
    await vi.waitFor(() => expect(readAgents).toHaveBeenCalledTimes(2));
    expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport,
      reports: [firstReport, secondReport],
      pending: true,
      failed: new Set([member]),
    });

    secondAgents.resolve({ value: [agent], install: vi.fn() });
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport,
      reports: [firstReport, secondReport],
      pending: false,
      failed: new Set(),
    }));
    // No stale failure is republished; both exact reports reached settlement.
    expect(statuses.mock.lastCall?.[0].reports[0]).toBe(firstReport);
    expect(statuses.mock.lastCall?.[0].reports[1]).toBe(secondReport);
    reconciler.retry();
    expect(readAgents).toHaveBeenCalledTimes(2);
    expect(readSources).toHaveBeenCalledTimes(member === "agents" ? 1 : 2);
  });

  it("continues a queued commit after failure, then retries only the remaining shared failure", async () => {
    const firstRead = deferred<{ value: AgentSupply[]; install: () => void }>();
    const secondReport = { ...report, chain: { ...report.chain, model_id: "second" } };
    const sources = deferred<{ value: Source[]; install: () => void }>();
    const statuses = vi.fn();
    const readAgents = vi.fn()
      .mockReturnValueOnce(firstRead.promise.then(() => { throw new Error("offline"); }))
      .mockResolvedValue({ value: [agent], install: vi.fn() });
    const readSources = vi.fn()
      .mockRejectedValueOnce(new Error("source offline"))
      .mockReturnValueOnce(sources.promise);
    const reconciler = createRouteProjectionReconciler({
      readAgents, readSources, onFailure: vi.fn(), onStatus: statuses,
    });
    reconciler.start(report);
    reconciler.start(secondReport);
    firstRead.resolve({ value: [agent], install: vi.fn() });
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport, reports: [report, secondReport],
      pending: false, failed: new Set(["sources"]),
    }));
    reconciler.retry();
    reconciler.retry();
    expect(readAgents).toHaveBeenCalledTimes(2);
    expect(readSources).toHaveBeenCalledTimes(2);
    expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport, reports: [report, secondReport],
      pending: true, failed: new Set(["sources"]),
    });
    sources.resolve({ value: [source], install: vi.fn() });
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport, reports: [report, secondReport],
      pending: false, failed: new Set(),
    }));
  });

  it("keeps both acquired failures when a later Agents read fails before Sources", async () => {
    const secondReport = { ...report, chain: { ...report.chain, model_id: "second" } };
    const statuses = vi.fn();
    const readAgents = vi.fn().mockResolvedValue({ value: [agent], install: vi.fn() });
    const readSources = vi.fn()
      .mockRejectedValueOnce(new Error("first Sources failed"))
      .mockResolvedValue({ value: [source], install: vi.fn() });
    const reconciler = createRouteProjectionReconciler({
      readAgents, readSources, onFailure: vi.fn(), onStatus: statuses,
    });
    reconciler.start(report);
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report, reports: [report], pending: false, failed: new Set(["sources"]),
    }));
    readAgents.mockRejectedValueOnce(new Error("second Agents failed"));
    reconciler.start(secondReport);
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport, reports: [report, secondReport],
      pending: false, failed: new Set(["agents", "sources"]),
    }));
    expect(readSources).toHaveBeenCalledTimes(1);
    reconciler.retry();
    await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
      report: secondReport, reports: [report, secondReport],
      pending: false, failed: new Set(),
    }));
    expect(readAgents).toHaveBeenCalledTimes(3);
    expect(readSources).toHaveBeenCalledTimes(2);
  });

  it("ignores an in-flight generation after page disposal", async () => {
    const agents = deferred<{ value: AgentSupply[]; install: () => void }>();
    const install = vi.fn();
    const readSources = vi.fn();
    const statuses = vi.fn();
    const reconciler = createRouteProjectionReconciler({
      readAgents: () => agents.promise, readSources,
      onFailure: vi.fn(), onStatus: statuses,
    });
    reconciler.start(report);
    reconciler.start({ ...report });
    reconciler.invalidate();
    agents.resolve({ value: [agent], install });
    await agents.promise;
    await Promise.resolve();
    expect(install).not.toHaveBeenCalled();
    expect(readSources).not.toHaveBeenCalled();
    expect(statuses).toHaveBeenCalledTimes(2);
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
        report: committed, reports: [committed], pending: true, failed: new Set(),
      });
      await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
        report: committed, reports: [committed], pending: false, failed: new Set(["sources"]),
      }));
      reconciler.retry();
      expect(statuses).toHaveBeenLastCalledWith({
        report: committed, reports: [committed], pending: true, failed: new Set(["sources"]),
      });
      sources.resolve({ value: [source], install: vi.fn() });
      await vi.waitFor(() => expect(statuses).toHaveBeenLastCalledWith({
        report: committed, reports: [committed], pending: false, failed: new Set(),
      }));
      for (const [status] of statuses.mock.calls) expect(status.report).toBe(committed);
    },
  );
});
