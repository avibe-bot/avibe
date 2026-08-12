// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import { afterEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import { ApiCallError, modelsApi } from "./modelsApi";
import {
  RouteChainDialog,
  type RouteCollectionObservation,
} from "./RouteChainDialog";
import { routeChainMatchesAttempt } from "./routeChainDraft";
import { readyRegion } from "./regionRead";
import type {
  AgentChain,
  AgentChainMutation,
  AgentSupply,
  Source,
} from "./types";

const agent: AgentSupply = {
  backend: "claude",
  cli_present: true,
  mode: "hub",
  menu_kind: "fixed",
};
const sources: Source[] = [
  {
    id: "src_a",
    last_discovered_at: null,
    kind: "api_key",
    vendor: "anthropic",
    display_name: "API key",
    protocol: "anthropic",
    supply_channel: "hub",
    billing: "metered",
    state: { status: "active", retry_at: null, detail_key: null },
    models: [],
  },
  {
    id: "src_b",
    last_discovered_at: null,
    kind: "subscription",
    vendor: "anthropic",
    display_name: "Claude subscription",
    protocol: "anthropic",
    supply_channel: "native_cli",
    billing: "monthly",
    state: { status: "active", retry_at: null, detail_key: null },
    models: [],
  },
];
const chain: AgentChain = {
  contract_version: 5,
  backend: "claude",
  model_id: "opus-5",
  current: { source_id: "src_b", model_id: "opus-5" },
  chain: [
    {
      source_id: "src_a",
      model_id: "claude-opus-5",
      channel: "hub",
      health: "cooldown",
      runnable: false,
      reason: null,
      retry_at: "2099-01-01T00:00:00Z",
    },
    {
      source_id: "src_b",
      model_id: "opus-5",
      channel: "native_cli",
      health: "healthy",
      runnable: true,
      reason: null,
      retry_at: null,
    },
  ],
  supply_state: "ok",
};
const mutation = (
  next: AgentChain = chain,
  report: Partial<Pick<AgentChainMutation, "removed_hops" | "interrupted">> =
    {},
): AgentChainMutation => ({
  chain: next,
  removed_hops: report.removed_hops ?? [],
  interrupted: report.interrupted ?? [],
});
const observation = <T,>(value: T) => ({ value, install: vi.fn() });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, reject, resolve };
};
const renderDialog = (onCommitted = vi.fn()) => {
  vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
  return render(
    <I18nextProvider i18n={i18n}>
      <RouteChainDialog
        selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
        sources={sources}
        onClose={vi.fn()}
        onCommitted={onCommitted}
        readAgents={vi.fn().mockResolvedValue(observation([agent]))}
        readSources={vi.fn().mockResolvedValue(observation(sources))}
      />
    </I18nextProvider>,
  );
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RouteChainDialog", () => {
  it("matches a suspended Direct attempt only by its exact ordered pairs", () => {
    const attempt = {
      backend: "claude" as const,
      modelId: "opus-5",
      stage: "initial" as const,
      submitted: [
        { source_id: "src_b", model_id: "opus-5" },
        { source_id: "src_a", model_id: "claude-opus-5" },
      ],
    };
    expect(
      routeChainMatchesAttempt(
        {
          ...chain,
          chain: [chain.chain[1], chain.chain[0]],
        },
        attempt,
      ),
    ).toBe(true);
    expect(routeChainMatchesAttempt(chain, attempt)).toBe(false);
  });
  it("renders the exact read projection and keeps the current hop identifiable", async () => {
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    expect(screen.getByText("opus-5 · Route chain")).toBeTruthy();
    const current = document.querySelector('[data-current="true"]');
    expect(current?.textContent).toContain("Claude subscription");
    expect(current?.textContent).toContain("opus-5");
    expect(screen.getAllByRole("button", { name: "Remove hop" })).toHaveLength(
      2,
    );
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("saves the changed ordered hop array and consumes the response envelope", async () => {
    const user = userEvent.setup();
    const onCommitted = vi.fn();
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockResolvedValue(mutation());
    renderDialog(onCommitted);
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("claude", "opus-5", {
        hops: [{ source_id: "src_b", model_id: "opus-5" }],
      }),
    );
    expect(onCommitted).toHaveBeenCalledWith({
      chain,
      removed_hops: [],
      interrupted: [],
    });
    expect(screen.getByText("Done").closest("button")).toBeTruthy();
  });

  it("renders the complete removed-hop impact after a successful save", async () => {
    const user = userEvent.setup();
    const removed = {
      backend: "claude" as const,
      menu_model: "opus-5",
      source_id: "src_a",
      model_id: "claude-opus-5",
      position: 1,
    };
    vi.spyOn(modelsApi, "putAgentChain").mockResolvedValue(
      mutation(chain, { removed_hops: [removed] }),
    );
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Claude Code · opus-5")).toBeTruthy();
    expect(screen.getByText("claude-opus-5 · Order #1")).toBeTruthy();
  });

  it("keeps Done available while page-owned M6 is pending and failed", async () => {
    const user = userEvent.setup();
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockResolvedValue(mutation());
    const onCommitted = vi.fn();
    const retry = vi.fn();
    vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
    const page = render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          onCommitted={onCommitted}
          commitReconciliation={{ pending: true, failed: false, retry }}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(
        "Route chain saved. Refreshing the model surface…",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Done").closest("button")).toBeTruthy();
    page.rerender(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          onCommitted={onCommitted}
          commitReconciliation={{ pending: false, failed: true, retry }}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    expect(
      await screen.findByText(
        "The route chain was saved, but the model surface could not be refreshed.",
      ),
    ).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("echoes the exact refusal plan on a forced confirmation", async () => {
    const user = userEvent.setup();
    const gap = {
      backend: "claude" as const,
      model_id: "opus-5",
      agents: ["writer"],
    };
    const hop = {
      backend: "claude" as const,
      menu_model: "opus-5",
      source_id: "src_a",
      model_id: "claude-opus-5",
      position: 1,
    };
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(
        new ApiCallError(
          "source_last_supplier",
          undefined,
          true,
          [gap],
          [],
          [hop],
        ),
      )
      .mockResolvedValueOnce(mutation());
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[1]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByRole("button", { name: "Save anyway" }),
    ).toBeTruthy();
    expect(screen.getByText("Save the route chain for opus-5")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Save anyway" }));

    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith("claude", "opus-5", {
        hops: [{ source_id: "src_a", model_id: "claude-opus-5" }],
        force: true,
        would_remove_hops: [hop],
        would_interrupt: [gap],
      }),
    );
  });

  it("re-reads the exact chain after an unconfirmed write and never retries the PUT", async () => {
    const user = userEvent.setup();
    const committedChain: AgentChain = {
      ...chain,
      current: { source_id: "src_b", model_id: "opus-5" },
      chain: [chain.chain[1]],
    };
    const read = vi
      .spyOn(modelsApi, "getAgentChain")
      .mockResolvedValueOnce(chain)
      .mockResolvedValueOnce(committedChain);
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(new TypeError("response lost"));
    renderDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(read).toHaveBeenCalledWith("claude", "opus-5"));
    expect(put).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Done").closest("button")).toBeTruthy();
  });

  it("installs a nonmatching D-36 observation without treating it as a retryable write", async () => {
    const user = userEvent.setup();
    const observed: AgentChain = {
      ...chain,
      chain: [chain.chain[0]],
      current: null,
      supply_state: "interrupted",
    };
    const read = vi
      .spyOn(modelsApi, "getAgentChain")
      .mockResolvedValueOnce(chain)
      .mockResolvedValueOnce(observed);
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(new TypeError("response lost"));
    const onObserved = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          onObserved={onObserved}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(put).toHaveBeenCalledTimes(1);
    expect(onObserved).toHaveBeenCalledWith(observed);
    expect(screen.getByText("The save outcome is not confirmed")).toBeTruthy();
  });

  it("runs D-36 Agents-first and retries only a failed Source member", async () => {
    const user = userEvent.setup();
    const nonmatching: AgentChain = {
      ...chain,
      chain: [chain.chain[0]],
      current: null,
      supply_state: "interrupted",
    };
    const agentRead = deferred<RouteCollectionObservation<AgentSupply[]>>();
    const readAgents = vi.fn().mockReturnValue(agentRead.promise);
    const readSources = vi
      .fn()
      .mockRejectedValueOnce(new Error("sources unread"))
      .mockResolvedValueOnce(observation(sources));
    const readChain = vi
      .spyOn(modelsApi, "getAgentChain")
      .mockResolvedValueOnce(chain)
      .mockResolvedValueOnce(nonmatching)
      .mockResolvedValueOnce(nonmatching);
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockRejectedValueOnce(new TypeError("response lost"));
    render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
          sources={sources}
          onClose={vi.fn()}
          readAgents={readAgents}
          readSources={readSources}
        />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getAllByRole("button", { name: "Remove hop" })[0]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(readChain).toHaveBeenCalledTimes(1);
    expect(readSources).not.toHaveBeenCalled();

    agentRead.resolve(observation([agent]));
    expect(
      await screen.findByText("The current model surface could not be read"),
    ).toBeTruthy();
    expect(readChain).toHaveBeenCalledTimes(2);
    expect(readSources).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(readSources).toHaveBeenCalledTimes(2));
    expect(readChain).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(readChain).toHaveBeenCalledTimes(3));
    expect(readSources).toHaveBeenCalledTimes(2);
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("supports the grip keyboard contract and restores the draft on Escape", async () => {
    const user = userEvent.setup();
    renderDialog();
    const grips = await screen.findAllByRole("button", {
      name: "Reorder this hop",
    });

    await user.click(grips[1]);
    await user.keyboard("{Space}");
    expect(grips[1].getAttribute("aria-grabbed")).toBe("true");
    await user.keyboard("{Escape}");
    expect(grips[1].getAttribute("aria-grabbed")).toBe("false");
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
