import { describe, expect, it, vi } from "vitest";

import { createRouteProjectionReconciler } from "./routeProjectionReconciliation";
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

    reconciler.start("claude");
    expect(readSources).not.toHaveBeenCalled();
    agents.resolve({ value: [agent], install: agentInstall });
    await vi.waitFor(() => expect(sourceInstall).toHaveBeenCalledTimes(1));
    expect(agentInstall).toHaveBeenCalledTimes(1);
    expect(readSources).toHaveBeenCalledTimes(1);
    expect(statuses).toHaveBeenLastCalledWith({
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

    reconciler.start("claude");

    await vi.waitFor(() => expect(sourceInstall).toHaveBeenCalledTimes(1));
    expect(agentInstall).toHaveBeenCalledTimes(1);
    expect(readSources).toHaveBeenCalledTimes(1);
    expect(statuses).toHaveBeenLastCalledWith({
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

    reconciler.start("claude");
    await vi.waitFor(() =>
      expect(statuses).toHaveBeenLastCalledWith({
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

    reconciler.start("claude");
    await vi.waitFor(() =>
      expect(statuses).toHaveBeenLastCalledWith({
        pending: false,
        failed: new Set(["sources"]),
      }),
    );
    reconciler.retry();
    await vi.waitFor(() => expect(readSources).toHaveBeenCalledTimes(2));
    expect(readAgents).toHaveBeenCalledTimes(1);
  });
});
