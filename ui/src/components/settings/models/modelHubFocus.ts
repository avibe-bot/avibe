import type { AgentBackend } from "./types";

const focusableSelector = [
  "button:not([disabled]):not([aria-disabled='true'])",
  "a[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex='0']",
].join(",");

const focusValid = (
  element: Element | null | undefined,
): element is HTMLElement =>
  element instanceof HTMLElement &&
  element.isConnected &&
  (element.matches(focusableSelector) || element.hasAttribute("tabindex")) &&
  !element.hasAttribute("disabled") &&
  element.getAttribute("aria-disabled") !== "true" &&
  !element.closest("[inert], [hidden]");

const exactModelRow = (
  root: HTMLElement,
  backend: AgentBackend,
  modelId: string,
): HTMLElement | null => {
  const row = [...root.querySelectorAll<HTMLElement>("[data-route-backend]")].find(
    (element) =>
      element.dataset.routeBackend === backend &&
      element.dataset.routeModel === modelId,
  );
  const opener = row?.querySelector<HTMLElement>(focusableSelector) ?? null;
  return focusValid(row) ? row : opener;
};

const exactGroupHead = (
  root: HTMLElement,
  backend: AgentBackend,
): HTMLElement | null =>
  [...root.querySelectorAll<HTMLElement>("[data-agent-group-head]")].find(
    (element) => element.dataset.agentGroupHead === backend,
  ) ?? null;

export const focusModelHubProjection = ({
  root,
  activeTarget,
  backend,
  modelId,
  preserveCurrentFocus = false,
}: {
  root: HTMLElement | null;
  activeTarget: HTMLElement | null;
  backend: AgentBackend;
  modelId: string;
  preserveCurrentFocus?: boolean;
}): HTMLElement | null => {
  if (!root) return null;
  const currentFocus =
    preserveCurrentFocus &&
    document.activeElement instanceof HTMLElement &&
    document.activeElement !== document.body
      ? document.activeElement
      : null;
  const candidates = [
    currentFocus,
    activeTarget,
    exactModelRow(root, backend, modelId),
    exactGroupHead(root, backend),
    root.querySelector<HTMLElement>(focusableSelector),
  ];
  const target = candidates.find(focusValid) ?? null;
  target?.focus();
  return target;
};

/**
 * Where keyboard focus goes when a dialog closes over the element that opened
 * it. A removed Source takes its own row with it, so the recorded return target
 * is disconnected by the time the dialog hands focus back; without a fallback
 * the browser drops focus on `document.body` and the next Tab restarts at the
 * top of the document instead of near the list the user was working in.
 *
 * `root` is the neighbourhood the caller nominates — the smallest surface still
 * standing around where that element was — so its first focusable is the closest
 * thing left to it.
 */
export const focusDialogReturn = ({
  root,
  returnTarget,
}: {
  root: HTMLElement | null;
  returnTarget: HTMLElement | null;
}): HTMLElement | null => {
  const candidates = [
    returnTarget,
    // Declared rather than 「first focusable」: the read that follows a removal
    // re-renders the list, so the first focusable at close time may itself be
    // the row that is about to go, and focus would fall to the body one tick
    // later. An anchor is chosen for outliving exactly that.
    root?.querySelector<HTMLElement>('[data-model-hub-focus-anchor]') ?? null,
    root?.querySelector<HTMLElement>(focusableSelector) ?? null,
  ];
  const target = candidates.find(focusValid) ?? null;
  target?.focus();
  return target;
};
