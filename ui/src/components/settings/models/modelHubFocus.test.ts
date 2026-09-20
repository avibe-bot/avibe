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
});
