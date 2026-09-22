// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { focusModelHubProjection } from "./modelHubFocus";

describe("model hub projection focus", () => {
  it("uses PF-1 after an active route row and its backend disappear", () => {
    const root = document.createElement("main");
    root.innerHTML = '<button data-destination="first">First control</button>';
    document.body.append(root);
    const removed = document.createElement("button");

    const focused = focusModelHubProjection({
      root,
      activeTarget: removed,
      backend: "claude",
      modelId: "model",
    });

    expect(focused).toBe(root.querySelector('[data-destination="first"]'));
    expect(document.activeElement).toBe(focused);
    root.remove();
  });

  it("prefers the exact model row and then the exact backend group", () => {
    const root = document.createElement("main");
    root.innerHTML = [
      '<button data-destination="first">First control</button>',
      '<div tabindex="-1" data-agent-group-head="claude">Claude</div>',
      '<button data-route-backend="claude" data-route-model="model">Model</button>',
    ].join("");
    document.body.append(root);

    const model = focusModelHubProjection({
      root,
      activeTarget: null,
      backend: "claude",
      modelId: "model",
    });
    expect(model?.dataset.routeModel).toBe("model");

    model?.remove();
    const group = focusModelHubProjection({
      root,
      activeTarget: null,
      backend: "claude",
      modelId: "model",
    });
    expect(group?.dataset.agentGroupHead).toBe("claude");
    root.remove();
  });

  it("preserves the current focus when a background projection installs", () => {
    const root = document.createElement("main");
    root.innerHTML = [
      '<button data-destination="current">Current</button>',
      '<button data-route-backend="claude" data-route-model="model">Model</button>',
    ].join("");
    document.body.append(root);
    const current = root.querySelector<HTMLElement>('[data-destination="current"]')!;
    current.focus();

    const focused = focusModelHubProjection({
      root,
      activeTarget: null,
      backend: "claude",
      modelId: "model",
      preserveCurrentFocus: true,
    });

    expect(focused).toBe(current);
    expect(document.activeElement).toBe(current);
    root.remove();
  });

  it("focuses the opener inside a remounted nonfocusable model row", () => {
    const root = document.createElement("main");
    root.innerHTML = [
      '<button data-destination="first">First control</button>',
      '<div data-route-backend="claude" data-route-model="模型">',
      '<button data-opener>Open route</button></div>',
    ].join("");
    document.body.append(root);
    const focused = focusModelHubProjection({
      root,
      activeTarget: document.createElement("button"),
      backend: "claude",
      modelId: "模型",
    });
    expect(focused).toBe(root.querySelector("[data-opener]"));
    expect(document.activeElement).toBe(focused);
    root.remove();
  });
});
