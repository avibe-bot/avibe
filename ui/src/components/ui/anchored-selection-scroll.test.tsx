// @vitest-environment jsdom
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Combobox } from "./combobox";

/**
 * An open modal Dialog locks page scrolling through `react-remove-scroll`, whose
 * document-level handler cancels every wheel event it does not recognise as
 * belonging to its own lock or one of its declared shards. The Dialog declares
 * only its own content as a shard, so an anchored selection surface portalled to
 * `document.body` — any Popover-based picker — falls outside both and loses its
 * wheel silently: the list keeps `overflow-y: auto` and a real `scrollHeight`,
 * `scrollTop` still moves when set programmatically, and only the user's wheel
 * does nothing. A CSS assertion therefore cannot see this defect at all; the
 * property has to be measured as an event outcome.
 *
 * The property: while a modal Dialog is open, the page stays locked and a
 * selection surface the Dialog opened still scrolls itself. Both halves are
 * asserted together, because the locked half is what keeps the scrollable half
 * from passing vacuously in a tree where no lock was ever installed.
 *
 * jsdom has no layout, so every element reports `scrollHeight === clientHeight`
 * and the lock legitimately cancels a wheel over a region that cannot scroll.
 * The list's scrollability is the browser's contribution to the scenario, so the
 * test states it explicitly instead of asserting through it — what is being
 * measured is which lock owns the event, not whether Tailwind emitted a class.
 */
const wheelPrevented = (node: Element): boolean => {
  const event = new WheelEvent("wheel", {
    bubbles: true,
    cancelable: true,
    deltaY: 240,
  });
  node.dispatchEvent(event);
  return event.defaultPrevented;
};

const declareScrollable = (node: HTMLElement) => {
  node.style.overflowY = "auto";
  Object.defineProperty(node, "scrollHeight", {
    value: 1000,
    configurable: true,
  });
  Object.defineProperty(node, "clientHeight", {
    value: 200,
    configurable: true,
  });
};

const outsideTheDialog = () => document.body.appendChild(document.createElement("div"));

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

describe("anchored selection surfaces inside a modal Dialog", () => {
  it("keeps the page locked while the surface itself still scrolls", async () => {
    const outside = outsideTheDialog();
    render(
      <DialogPrimitive.Root open>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay />
          <DialogPrimitive.Content aria-label="host dialog">
            <Combobox
              options={[
                { value: "a", label: "first" },
                { value: "b", label: "second" },
              ]}
              value=""
              onValueChange={vi.fn()}
              placeholder="pick one"
            />
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>,
    );

    await userEvent.click(screen.getByRole("combobox"));
    const option = await screen.findByText("first");
    const list = document.querySelector<HTMLElement>("[cmdk-list]");
    expect(list).not.toBeNull();
    declareScrollable(list as HTMLElement);

    expect(wheelPrevented(option)).toBe(false);
    expect(wheelPrevented(outside)).toBe(true);
  });
});
