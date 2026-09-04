// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  contract_version: 8,
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
/** An Agent whose eligible sources actually carry spare models — the only shape
 *  that makes the add-hop selector reachable, since `routeCandidates` excludes
 *  the pairs the draft already holds. */
const stocked = () => ({
  agent: {
    ...agent,
    sources: {
      order: ["src_a", "src_b"],
      eligibility: [
        { source_id: "src_a", eligible: true },
        { source_id: "src_b", eligible: true },
      ],
    },
  } satisfies AgentSupply,
  sources: [
    {
      ...sources[0],
      models: [
        { id: "claude-opus-5", origin: "discovered", reasoning_efforts: [] },
        { id: "claude-sonnet-5", origin: "discovered", reasoning_efforts: [] },
        { id: "claude-haiku-5", origin: "discovered", reasoning_efforts: [] },
      ],
    },
    {
      ...sources[1],
      models: [
        { id: "opus-5", origin: "discovered", reasoning_efforts: [] },
        { id: "sonnet-5", origin: "discovered", reasoning_efforts: [] },
      ],
    },
  ] satisfies Source[],
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
const renderStockedDialog = () => {
  const fixture = stocked();
  vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
  render(
    <I18nextProvider i18n={i18n}>
      <RouteChainDialog
        selection={{
          agent: fixture.agent,
          modelId: "opus-5",
          read: readyRegion(chain),
        }}
        sources={fixture.sources}
        onClose={vi.fn()}
        onCommitted={vi.fn()}
        readAgents={vi.fn().mockResolvedValue(observation([fixture.agent]))}
        readSources={vi.fn().mockResolvedValue(observation(fixture.sources))}
      />
    </I18nextProvider>,
  );
};
const renderDialog = (onCommitted = vi.fn(), onClose = vi.fn()) => {
  vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
  return render(
    <I18nextProvider i18n={i18n}>
      <RouteChainDialog
        selection={{ agent, modelId: "opus-5", read: readyRegion(chain) }}
        sources={sources}
        onClose={onClose}
        onCommitted={onCommitted}
        readAgents={vi.fn().mockResolvedValue(observation([agent]))}
        readSources={vi.fn().mockResolvedValue(observation(sources))}
      />
    </I18nextProvider>,
  );
};

beforeEach(() => {
  // cmdk observes its list box and scrolls the active row into view; jsdom
  // implements neither.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
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
    const ordinals = [...document.querySelectorAll(".model-hub-route-ordinal")];
    expect(ordinals[0]?.className).toContain("model-hub-accent-pill--mint");
    expect(ordinals[1]?.className).toContain("model-hub-fill-0a");
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

  it("MH-ROUTE-EDIT-001 replaces one hop in place and saves the exact order", async () => {
    const user = userEvent.setup();
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockResolvedValue(mutation());
    renderStockedDialog();
    const editButtons = await screen.findAllByRole("button", {
      name: "Edit hop",
    });

    await user.click(editButtons[0]);
    expect(
      [...document.querySelectorAll(".model-hub-route-candidate-model")].map(
        (element) => element.textContent,
      ),
    ).toEqual([
      "claude-haiku-5",
      "claude-opus-5",
      "claude-sonnet-5",
      "sonnet-5",
    ]);
    await user.type(
      screen.getByPlaceholderText("Search sources or models"),
      "claude-sonnet-5",
    );
    await user.click(screen.getByRole("button", { name: "Replace" }));

    await waitFor(() =>
      expect(
        [...document.querySelectorAll(".model-hub-route-hop-model")].map(
          (element) => element.textContent,
        ),
      ).toEqual(["claude-sonnet-5", "opus-5"]),
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("claude", "opus-5", {
        hops: [
          { source_id: "src_a", model_id: "claude-sonnet-5" },
          { source_id: "src_b", model_id: "opus-5" },
        ],
      }),
    );
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
    const removed = {
      backend: "claude" as const,
      menu_model: "opus-5",
      source_id: "src_a",
      model_id: "claude-opus-5",
      position: 1,
    };
    const put = vi
      .spyOn(modelsApi, "putAgentChain")
      .mockResolvedValue(mutation(chain, { removed_hops: [removed] }));
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
    expect(
      screen.getByText(
        "These are the items this save actually removed or interrupted.",
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
    expect(
      screen.getByText(
        "These are the items this save actually removed or interrupted.",
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
    expect(screen.getByText("Hops that will be removed")).toBeTruthy();
    expect(screen.getByText("1 hop")).toBeTruthy();
    expect(
      screen.getByText((_, element) =>
        element?.classList.contains("model-hub-guard-hop") === true &&
        element.textContent?.includes("Order #1") === true,
      ),
    ).toBeTruthy();
    expect(
      screen.getByText("Some models will be left with no usable source."),
    ).toBeTruthy();
    expect(
      screen.queryByText("Models that will be left with no source"),
    ).toBeNull();
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

  it("returns an invalidated pending draft to editing after a named rejection", async () => {
    const user = userEvent.setup();
    const pending = deferred<AgentChainMutation>();
    const { agent: editableAgent, sources: editableSources } = stocked();
    vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(chain);
    vi.spyOn(modelsApi, "putAgentChain").mockReturnValue(pending.promise);
    const props = {
      selection: {
        agent: editableAgent,
        modelId: "opus-5",
        read: readyRegion(chain),
      },
      onClose: vi.fn(),
      readAgents: vi.fn().mockResolvedValue(observation([editableAgent])),
      readSources: vi.fn().mockResolvedValue(observation(editableSources)),
    };
    const page = render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog {...props} sources={editableSources} />
      </I18nextProvider>,
    );
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getByRole("button", { name: "Add a hop" }));
    await user.click(screen.getByRole("button", { name: "Add" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findAllByText("Saving route chain…")).toHaveLength(2);

    const refreshedSources = editableSources.map((source) =>
      source.id === "src_a"
        ? { ...source, models: source.models.slice(0, 1) }
        : source,
    );
    page.rerender(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog {...props} sources={refreshedSources} />
      </I18nextProvider>,
    );
    await act(async () => {
      pending.reject(new ApiCallError("invalid_route"));
      await pending.promise.catch(() => undefined);
    });

    expect(
      await screen.findAllByText(
        "This edited hop is unavailable after the refresh. Replace or remove it before saving.",
      ),
    ).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Remove hop" })).toHaveLength(3);
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("names both candidate columns and prints each source once per group", async () => {
    const user = userEvent.setup();
    renderStockedDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });

    await user.click(screen.getByRole("button", { name: "Add a hop" }));

    expect(screen.getByText("Source")).toBeTruthy();
    expect(screen.getByText("Model")).toBeTruthy();
    // Both halves of a candidate are legible: the model id AND which source
    // supplies it. The source column is what a sizing rework once dropped, so
    // the assertion is over the rendered row grid, not over the mere presence
    // of the name somewhere in the panel.
    const rows = [...document.querySelectorAll(".model-hub-route-candidate")];
    expect(
      rows.map((row) => [
        row.querySelector(".model-hub-route-candidate-source")?.textContent,
        row.querySelector(".model-hub-route-candidate-model")?.textContent,
      ]),
    ).toEqual([
      ["API key", "claude-haiku-5"],
      ["", "claude-sonnet-5"],
      ["Claude subscription", "sonnet-5"],
    ]);
  });

  it("narrows the candidates to what was typed without claiming none exist", async () => {
    const user = userEvent.setup();
    renderStockedDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getByRole("button", { name: "Add a hop" }));

    await user.type(
      screen.getByPlaceholderText("Search sources or models"),
      "subscription",
    );

    expect(
      [...document.querySelectorAll(".model-hub-route-candidate-model")].map(
        (cell) => cell.textContent,
      ),
    ).toEqual(["sonnet-5"]);

    await user.clear(screen.getByPlaceholderText("Search sources or models"));
    await user.type(
      screen.getByPlaceholderText("Search sources or models"),
      "gpt",
    );

    expect(screen.getByText("No source or model matches that")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Add" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("keeps the candidate list scrollable while the dialog locks the page", async () => {
    const user = userEvent.setup();
    renderStockedDialog();
    await screen.findAllByRole("button", { name: "Remove hop" });
    await user.click(screen.getByRole("button", { name: "Add a hop" }));

    // The dialog locks page scrolling through `react-remove-scroll`, which
    // cancels wheel events outside its own lock and shards. The selector is
    // portalled to `document.body`, so it is outside both unless it owns a lock
    // of its own — and when it does not, the panel keeps its `overflow-y: auto`
    // and its overflowing content while the wheel silently does nothing. Only
    // the event outcome can see that, so it is measured here rather than
    // inferred from the CSS. jsdom has no layout, so the scrollability the
    // browser gets from that CSS is stated explicitly.
    const list = document.querySelector<HTMLElement>(
      ".model-hub-route-selector-list",
    );
    expect(list).not.toBeNull();
    (list as HTMLElement).style.overflowY = "auto";
    Object.defineProperty(list, "scrollHeight", {
      value: 1000,
      configurable: true,
    });
    Object.defineProperty(list, "clientHeight", {
      value: 200,
      configurable: true,
    });
    const wheelPrevented = (node: Element) => {
      const event = new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        deltaY: 240,
      });
      node.dispatchEvent(event);
      return event.defaultPrevented;
    };

    expect(
      wheelPrevented(document.querySelector(".model-hub-route-candidate")!),
    ).toBe(false);
    expect(
      wheelPrevented(document.body.appendChild(document.createElement("div"))),
    ).toBe(true);
  });

  it("announces the one-based position of the hop focused after removal", async () => {
    const user = userEvent.setup();
    const threeHopChain: AgentChain = {
      ...chain,
      chain: [
        chain.chain[0],
        chain.chain[1],
        {
          ...chain.chain[0],
          source_id: "src_c",
          model_id: "claude-haiku-5",
        },
      ],
    };
    vi.spyOn(modelsApi, "getAgentChain").mockResolvedValue(threeHopChain);
    render(
      <I18nextProvider i18n={i18n}>
        <RouteChainDialog
          selection={{ agent, modelId: "opus-5", read: readyRegion(threeHopChain) }}
          sources={sources}
          onClose={vi.fn()}
          readAgents={vi.fn().mockResolvedValue(observation([agent]))}
          readSources={vi.fn().mockResolvedValue(observation(sources))}
        />
      </I18nextProvider>,
    );
    const removeButtons = await screen.findAllByRole("button", {
      name: "Remove hop",
    });

    await user.click(removeButtons[1]);

    expect(screen.getByText("Moved to hop 2.")).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getAllByRole("button", { name: "Reorder this hop" })[1],
      ),
    );
  });

  it("cancels only the active grab and preserves the earlier unsaved draft", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDialog(vi.fn(), onClose);
    const grips = await screen.findAllByRole("button", {
      name: "Reorder this hop",
    });

    await user.click(grips[0]);
    await user.keyboard("{Space}");
    await user.keyboard("{ArrowDown}");
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getAllByRole("button", { name: "Reorder this hop" })[1],
      ),
    );
    await user.keyboard("{Space}");
    expect(
      screen.getAllByRole("button", { name: "Reorder this hop" })[1]
        .getAttribute("aria-grabbed"),
    ).toBe("false");
    const unsavedOrder = ["Claude subscription", "API key"];
    expect(
      [...document.querySelectorAll(".model-hub-route-hop-name")].map(
        (node) => node.textContent,
      ),
    ).toEqual(unsavedOrder);
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);

    await user.keyboard("{Space}");
    expect(
      screen.getAllByRole("button", { name: "Reorder this hop" })[1]
        .getAttribute("aria-grabbed"),
    ).toBe("true");
    await user.keyboard("{ArrowUp}");
    expect(screen.getByText("Moved to hop 1.")).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getAllByRole("button", { name: "Reorder this hop" })[0],
      ),
    );
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(
      screen.getByText("Reorder cancelled. Restored to hop 2."),
    ).toBeTruthy();
    expect(
      [...document.querySelectorAll(".model-hub-route-hop-name")].map(
        (node) => node.textContent,
      ),
    ).toEqual(unsavedOrder);
    const restoredGrips = screen.getAllByRole("button", {
      name: "Reorder this hop",
    });
    expect(restoredGrips[1].getAttribute("aria-grabbed")).toBe("false");
    await waitFor(() => expect(document.activeElement).toBe(restoredGrips[1]));
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("keeps the native Escape dismissal when no hop is grabbed", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderDialog(vi.fn(), onClose);
    await screen.findAllByRole("button", { name: "Reorder this hop" });

    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
