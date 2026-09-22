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
