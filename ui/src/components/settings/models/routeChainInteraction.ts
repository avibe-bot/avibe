import type { RouteHop } from "./types";

type GrabState = {
  index: number;
  originIndex: number;
  snapshot: RouteHop[];
};

export type RouteChainInteraction = {
  draft: RouteHop[];
  focusIndex: number;
  grab: GrabState | null;
};

export type RouteChainInteractionAction =
  | { type: "reset"; draft: RouteHop[]; focusIndex?: number }
  | { type: "focus"; index: number }
  | { type: "append"; hop: RouteHop }
  | { type: "replace"; index: number; hop: RouteHop }
  | { type: "remove"; index: number }
  | { type: "begin-grab"; index: number }
  | { type: "move-grab"; direction: -1 | 1 }
  | { type: "move-grab-edge"; edge: "start" | "end" }
  | { type: "move-grab-to"; index: number }
  | { type: "drop-grab" }
  | { type: "cancel-grab" };

const cloneDraft = (draft: RouteHop[]): RouteHop[] =>
  draft.map((hop) => ({ ...hop }));

const boundedIndex = (index: number, length: number): number =>
  length === 0 ? 0 : Math.max(0, Math.min(length - 1, index));

const moveGrabbed = (
  state: RouteChainInteraction,
  targetIndex: number,
): RouteChainInteraction => {
  if (!state.grab || state.draft.length === 0) return state;
  const nextIndex = boundedIndex(targetIndex, state.draft.length);
  if (nextIndex === state.grab.index) return state;
  const draft = [...state.draft];
  const [moved] = draft.splice(state.grab.index, 1);
  draft.splice(nextIndex, 0, moved);
  return {
    draft,
    focusIndex: nextIndex,
    grab: { ...state.grab, index: nextIndex },
  };
};

export const createRouteChainInteraction = (
  draft: RouteHop[] = [],
): RouteChainInteraction => ({
  draft: cloneDraft(draft),
  focusIndex: 0,
  grab: null,
});

export function advanceRouteChainInteraction(
  state: RouteChainInteraction,
  action: RouteChainInteractionAction,
): RouteChainInteraction {
  switch (action.type) {
    case "reset":
      return {
        draft: cloneDraft(action.draft),
        focusIndex: boundedIndex(
          action.focusIndex ?? state.focusIndex,
          action.draft.length,
        ),
        grab: null,
      };
    case "focus":
      return {
        ...state,
        focusIndex: boundedIndex(action.index, state.draft.length),
      };
    case "append":
      return {
        draft: [...state.draft, { ...action.hop }],
        focusIndex: state.draft.length,
        grab: null,
      };
    case "replace":
      if (action.index < 0 || action.index >= state.draft.length) return state;
      return {
        draft: state.draft.map((hop, index) =>
          index === action.index ? { ...action.hop } : hop,
        ),
        focusIndex: action.index,
        grab: null,
      };
    case "remove": {
      const draft = state.draft.filter((_, index) => index !== action.index);
      return {
        draft,
        focusIndex: boundedIndex(action.index, draft.length),
        grab: null,
      };
    }
    case "begin-grab": {
      if (state.draft.length === 0) return state;
      const index = boundedIndex(action.index, state.draft.length);
      return {
        ...state,
        focusIndex: index,
        grab: {
          index,
          originIndex: index,
          snapshot: cloneDraft(state.draft),
        },
      };
    }
    case "move-grab":
      return moveGrabbed(
        state,
        (state.grab?.index ?? state.focusIndex) + action.direction,
      );
    case "move-grab-edge":
      return moveGrabbed(
        state,
        action.edge === "start" ? 0 : state.draft.length - 1,
      );
    case "move-grab-to":
      return moveGrabbed(state, action.index);
    case "drop-grab":
      return state.grab ? { ...state, grab: null } : state;
    case "cancel-grab":
      return state.grab
        ? {
            draft: cloneDraft(state.grab.snapshot),
            focusIndex: boundedIndex(
              state.grab.originIndex,
              state.grab.snapshot.length,
            ),
            grab: null,
          }
        : state;
  }
}
