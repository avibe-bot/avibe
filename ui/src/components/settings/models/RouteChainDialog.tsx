import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  GripVertical,
  Info,
  ListOrdered,
  LoaderCircle,
  Plus,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { GuardGapList } from "./GuardGapList";
import { equalHopIdentity, hopBelongsToSource } from "./hopIdentity";
import { apiFailure, modelsApi, type GuardConfirmation } from "./modelsApi";
import type { ModelChainRead } from "./modelRows";
import {
  routeCandidates,
  reorderRouteDraft,
  sameRouteDraft,
  validateRouteDraft,
  type RouteCandidate,
} from "./routeChainDraft";
import {
  advanceRouteChainInteraction,
  createRouteChainInteraction,
  type RouteChainInteractionAction,
} from "./routeChainInteraction";
import type {
  AgentChainMutation,
  AgentSupply,
  RouteHop,
  RouteHopRef,
  Source,
} from "./types";

const sourceName = (sources: Source[], sourceId: string): string =>
  sources.find((source) => source.id === sourceId)?.display_name ?? sourceId;

/** Stable list-item identity for one candidate pair. Nothing parses it back. */
const candidateKey = (hop: RouteHop): string =>
  JSON.stringify([hop.source_id, hop.model_id]);

type GuardState = {
  hops: RouteHopRef[];
  gaps: NonNullable<GuardConfirmation["would_interrupt"]>;
};
export type RouteReport = {
  chain: AgentChainMutation["chain"];
  removed_hops: RouteHopRef[] | null;
  interrupted: AgentChainMutation["interrupted"] | null;
};
type Phase =
  | "loading"
  | "ready"
  | "unread"
  | "saving"
  | "guard"
  | "rejected"
  | "unknown"
  | "impact"
  | "refreshing"
  | "reconciling"
  | "direct";
type ReconcileMember = "agents" | "sources" | "chain";
type UnknownObservation = "none" | "nonmatching" | "read_failed";

export type RouteChainSelection = {
  agent: AgentSupply;
  modelId: string;
  read: ModelChainRead | undefined;
  available?: boolean;
};
export type RouteCollectionObservation<T> = {
  value: T;
  install: () => void;
};
export type RouteCommitReconciliation = {
  pending: boolean;
  failed: boolean;
  retry: () => void;
};
export type SuspendedRouteAttempt = {
  backend: AgentSupply["backend"];
  modelId: string;
  stage: "initial" | "confirmed";
  submitted: RouteHop[];
};

export const RouteChainDialog: React.FC<{
  selection: RouteChainSelection | null;
  sources: Source[];
  onClose: () => void;
  onCommitted?: (result: RouteReport) => void;
  commitReconciliation?: RouteCommitReconciliation | null;
  onObserved?: (chain: AgentChainMutation["chain"]) => void;
  readAgents: () => Promise<RouteCollectionObservation<AgentSupply[]>>;
  readSources: () => Promise<RouteCollectionObservation<Source[]>>;
  onDirectMode?: (
    attempt: SuspendedRouteAttempt | null,
    observedAgent: AgentSupply | null,
  ) => void;
}> = ({
  selection,
  sources,
  onClose,
  onCommitted,
  commitReconciliation,
  onObserved,
  readAgents,
  readSources,
  onDirectMode,
}) => {
  const { t } = useTranslation();
  const [phase, setPhase] = React.useState<Phase>("loading");
  const [origin, setOrigin] = React.useState<RouteHop[]>([]);
  const interactionRef = React.useRef(createRouteChainInteraction());
  const [interaction, setInteraction] = React.useState(interactionRef.current);
  const [chain, setChain] = React.useState<AgentChainMutation["chain"] | null>(
    null,
  );
  const [guard, setGuard] = React.useState<GuardState | null>(null);
  const [submitted, setSubmitted] = React.useState<RouteHop[] | null>(null);
  const [submittedStage, setSubmittedStage] =
    React.useState<SuspendedRouteAttempt["stage"]>("initial");
  const [report, setReport] = React.useState<RouteReport | null>(null);
  const [reconcileFailed, setReconcileFailed] = React.useState(false);
  const [failedMembers, setFailedMembers] = React.useState<
    ReadonlySet<ReconcileMember>
  >(() => new Set());
  const [unknownObservation, setUnknownObservation] =
    React.useState<UnknownObservation>("none");
  const [unknownSourceCurrent, setUnknownSourceCurrent] = React.useState(false);
  const [selectorOpen, setSelectorOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [candidate, setCandidate] = React.useState<RouteCandidate | null>(null);
  const [announcement, setAnnouncement] = React.useState<{
    key: string;
    params?: Record<string, unknown>;
  } | null>(null);
  const gripRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const removeRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const searchRef = React.useRef<HTMLInputElement | null>(null);
  const addButtonRef = React.useRef<HTMLButtonElement | null>(null);
  const reseedButtonRef = React.useRef<HTMLButtonElement | null>(null);
  const cancelButtonRef = React.useRef<HTMLButtonElement | null>(null);
  const saveButtonRef = React.useRef<HTMLButtonElement | null>(null);
  const savingStatusRef = React.useRef<HTMLSpanElement | null>(null);
  const retryButtonRef = React.useRef<HTMLButtonElement | null>(null);
  const doneButtonRef = React.useRef<HTMLButtonElement | null>(null);
  const openedIdentity = React.useRef<string | null>(null);
  const invalidSignature = React.useRef("");
  const generation = React.useRef(0);

  const agent = selection?.agent ?? null;
  const modelId = selection?.modelId ?? "";
  const selectionBackend = selection?.agent.backend;
  const draft = interaction.draft;
  const rovingIndex = interaction.focusIndex;
  const grabbed = interaction.grab?.index ?? null;
  const advanceInteraction = React.useCallback(
    (action: RouteChainInteractionAction) => {
      const next = advanceRouteChainInteraction(interactionRef.current, action);
      interactionRef.current = next;
      setInteraction(next);
      return next;
    },
    [],
  );
  const candidates = agent ? routeCandidates(agent, sources, draft) : [];
  // The filter narrows the same grouped projection V5 exposes, so an empty
  // result means "nothing matches what you typed", never "nothing is available"
  // — that second sentence stays `route.add.none` on the disabled trigger.
  const term = query.trim().toLowerCase();
  const matched = term
    ? candidates.filter((item) =>
        `${item.source.display_name}\n${item.hop.model_id}`
          .toLowerCase()
          .includes(term),
      )
    : candidates;
  const candidateGroups = matched.reduce<
    Array<{ source: Source; items: RouteCandidate[] }>
  >((groups, item) => {
    const previous = groups.at(-1);
    if (previous?.source.id === item.source.id) previous.items.push(item);
    else groups.push({ source: item.source, items: [item] });
    return groups;
  }, []);
  const matchedRef = React.useRef(matched);
  matchedRef.current = matched;
  const matchedKeys = matched.map((item) => candidateKey(item.hop)).join("\n");
  const backend = agent
    ? (t(`settings.models.backends.${agent.backend}`, {
        defaultValue: agent.backend,
      }) as string)
    : "";
  const valid = agent
    ? validateRouteDraft(agent, sources, origin, draft)
    : { invalidIndexes: [], valid: false };
  const dirty = !sameRouteDraft(origin, draft);
  const announce = (key: string, params?: Record<string, unknown>) =>
    setAnnouncement({ key, params });
  const focusAfterRender = (ref: React.RefObject<HTMLElement | null>) => {
    requestAnimationFrame(() => ref.current?.focus());
  };

  const readChain = React.useCallback(async () => {
    if (!selectionBackend) return;
    const token = ++generation.current;
    setPhase("loading");
    try {
      const next = await modelsApi.getAgentChain(selectionBackend, modelId);
      if (token !== generation.current) return;
      const hops = next.chain.map(({ source_id, model_id }) => ({
        source_id,
        model_id,
      }));
      setChain(next);
      onObserved?.(next);
      setOrigin(hops);
      advanceInteraction({ type: "reset", draft: hops, focusIndex: 0 });
      setGuard(null);
      setPhase("ready");
    } catch (error) {
      if (token !== generation.current) return;
      if (apiFailure(error)?.code === "direct_mode") {
        setPhase("direct");
        onDirectMode?.(null, null);
        return;
      }
      setPhase("unread");
      requestAnimationFrame(() => retryButtonRef.current?.focus());
    }
  }, [advanceInteraction, modelId, onDirectMode, onObserved, selectionBackend]);

  React.useEffect(() => {
    const nextIdentity = selection
      ? `${selection.agent.backend}\u0000${selection.modelId}`
      : null;
    if (nextIdentity === openedIdentity.current) return;
    openedIdentity.current = nextIdentity;
    generation.current += 1;
    setGuard(null);
    setSubmitted(null);
    setSubmittedStage("initial");
    setReport(null);
    setReconcileFailed(false);
    setFailedMembers(new Set());
    setUnknownObservation("none");
    setUnknownSourceCurrent(false);
    setSelectorOpen(false);
    setQuery("");
    setCandidate(null);
    advanceInteraction({ type: "reset", draft: [], focusIndex: 0 });
    if (selection) void readChain();
  }, [advanceInteraction, readChain, selection]);

  React.useEffect(() => {
    if (
      !selection ||
      selection.available !== false ||
      phase === "impact" ||
      phase === "refreshing"
    ) {
      return;
    }
    generation.current += 1;
    onClose();
  }, [onClose, phase, selection]);

  // ET-5a plus its invariant: while the selector is open exactly one candidate is
  // active, and it is always one of the candidates currently listed. Filtering or
  // a draft change that drops the active pair re-elects the first listed one
  // rather than leaving the confirmation pointing at a row nobody can see.
  React.useEffect(() => {
    if (!selectorOpen) return;
    setCandidate((current) => {
      const keys = matchedKeys ? matchedKeys.split("\n") : [];
      if (current && keys.includes(candidateKey(current.hop))) return current;
      return matchedRef.current[0] ?? null;
    });
  }, [matchedKeys, selectorOpen]);

  React.useEffect(() => {
    const signature = valid.invalidIndexes.join(",");
    if (!signature) {
      invalidSignature.current = "";
      return;
    }
    if (phase !== "ready" && phase !== "rejected") return;
    if (signature === invalidSignature.current) return;
    invalidSignature.current = signature;
    if (phase === "rejected") setPhase("ready");
    setSelectorOpen(false);
    setQuery("");
    setCandidate(null);
    advanceInteraction({ type: "drop-grab" });
    focusAfterRender({
      current: removeRefs.current[valid.invalidIndexes[0]] ?? null,
    });
  }, [advanceInteraction, phase, valid.invalidIndexes]);

  React.useEffect(() => {
    if (!report || !commitReconciliation) return;
    setReconcileFailed(commitReconciliation.failed);
    setPhase(commitReconciliation.pending ? "refreshing" : "impact");
    if (commitReconciliation.failed) focusAfterRender(retryButtonRef);
  }, [commitReconciliation, report]);

  const focusAfterSelectorClose = () => {
    requestAnimationFrame(() => addButtonRef.current?.focus());
  };

  const close = () => {
    if (phase === "guard") {
      setGuard(null);
      setPhase("ready");
      focusAfterRender(saveButtonRef);
      return;
    }
    if (phase !== "saving") {
      if (phase !== "impact" && phase !== "refreshing") {
        generation.current += 1;
      }
      onClose();
    }
  };
  const remove = (index: number) => {
    if (phase === "saving" || phase === "impact" || phase === "refreshing")
      return;
    const next = advanceInteraction({ type: "remove", index });
    const nextDraft = next.draft;
    const focusedIndex = next.focusIndex;
    requestAnimationFrame(() => {
      if (nextDraft.length > 0) {
        gripRefs.current[focusedIndex]?.focus();
      } else if (candidates.length > 0) {
        addButtonRef.current?.focus();
      } else if (reseedButtonRef.current && !reseedButtonRef.current.disabled) {
        reseedButtonRef.current.focus();
      } else {
        cancelButtonRef.current?.focus();
      }
    });
    if (nextDraft.length > 0) {
      announce("settings.models.routeDialog.reorder.position", {
        position: focusedIndex + 1,
      });
    }
  };
  const moveGrab = (action: RouteChainInteractionAction) => {
    const previous = interactionRef.current;
    const next = advanceInteraction(action);
    if (next.focusIndex === previous.focusIndex) return;
    requestAnimationFrame(() => gripRefs.current[next.focusIndex]?.focus());
    announce("settings.models.routeDialog.reorder.position", {
      position: next.focusIndex + 1,
    });
  };
  const onGripKeyDown = (
    event: React.KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    if (event.key === "Tab" && grabbed !== null) {
      const next = advanceInteraction({ type: "drop-grab" });
      announce("settings.models.routeDialog.reorder.dropped", {
        position: next.focusIndex + 1,
      });
    } else if (
      event.key === " " ||
      event.key === "Space" ||
      event.key === "Spacebar" ||
      event.code === "Space"
    ) {
      event.preventDefault();
      if (grabbed === index) {
        const next = advanceInteraction({ type: "drop-grab" });
        announce("settings.models.routeDialog.reorder.dropped", {
          position: next.focusIndex + 1,
        });
      } else {
        const next = advanceInteraction({ type: "begin-grab", index });
        announce("settings.models.routeDialog.reorder.grabbed", {
          position: next.focusIndex + 1,
        });
      }
    } else if (event.key === "Escape" && grabbed !== null) {
      event.preventDefault();
      const next = advanceInteraction({ type: "cancel-grab" });
      requestAnimationFrame(() => gripRefs.current[next.focusIndex]?.focus());
      announce("settings.models.routeDialog.reorder.cancelled", {
        position: next.focusIndex + 1,
      });
    } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      event.preventDefault();
      if (grabbed === null) {
        const target = Math.max(
          0,
          Math.min(draft.length - 1, index + (event.key === "ArrowUp" ? -1 : 1)),
        );
        const next = advanceInteraction({ type: "focus", index: target });
        gripRefs.current[next.focusIndex]?.focus();
      } else {
        moveGrab({
          type: "move-grab",
          direction: event.key === "ArrowUp" ? -1 : 1,
        });
      }
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      if (grabbed === null) {
        const next = advanceInteraction({
          type: "focus",
          index: event.key === "Home" ? 0 : draft.length - 1,
        });
        gripRefs.current[next.focusIndex]?.focus();
      } else {
        moveGrab({
          type: "move-grab-edge",
          edge: event.key === "Home" ? "start" : "end",
        });
      }
    }
  };
  const beginCommitted = (
    nextReport: RouteReport,
    committedHops: RouteHop[],
  ) => {
    generation.current += 1;
    setChain(nextReport.chain);
    setReport(nextReport);
    setOrigin(committedHops);
    advanceInteraction({ type: "reset", draft: committedHops });
    setUnknownObservation("none");
    setUnknownSourceCurrent(false);
    onCommitted?.(nextReport);
    setPhase(
      nextReport.removed_hops?.length || nextReport.interrupted?.length
        ? "impact"
        : "refreshing",
    );
    focusAfterRender(doneButtonRef);
  };

  const submit = async (confirmation?: GuardState) => {
    if (!selection || !agent || phase === "saving" || !valid.valid || !dirty)
      return;
    const hops = (confirmation ? (submitted ?? draft) : draft).map((hop) => ({
      ...hop,
    }));
    setSubmitted(hops);
    setSubmittedStage(confirmation ? "confirmed" : "initial");
    setPhase("saving");
    setGuard(null);
    requestAnimationFrame(() => savingStatusRef.current?.focus());
    try {
      const result = await modelsApi.putAgentChain(agent.backend, modelId, {
        hops,
        ...(confirmation
          ? {
              force: true as const,
              would_remove_hops: confirmation.hops,
              would_interrupt: confirmation.gaps,
            }
          : {}),
      });
      beginCommitted(
        {
          chain: result.chain,
          removed_hops: result.removed_hops,
          interrupted: result.interrupted,
        },
        hops,
      );
    } catch (error) {
      const failure = apiFailure(error);
      if (failure?.code === "direct_mode") {
        setPhase("direct");
        onDirectMode?.(
          {
            backend: agent.backend,
            modelId,
            stage: confirmation ? "confirmed" : "initial",
            submitted: hops,
          },
          null,
        );
        return;
      }
      if (
        failure?.code === "source_last_supplier" &&
        failure.wouldInterrupt.length > 0
      ) {
        setGuard({
          hops: failure.wouldRemoveHops,
          gaps: failure.wouldInterrupt,
        });
        setPhase("guard");
        focusAfterRender(cancelButtonRef);
        return;
      }
      const nextPhase = failure?.serverNamed ? "rejected" : "unknown";
      setPhase(nextPhase);
      focusAfterRender(retryButtonRef);
    }
  };

  const reconcileUnknownMembers = async (
    token: number,
    members: ReadonlySet<ReconcileMember>,
  ) => {
    if (!selectionBackend || !submitted) return;
    const [sourceAnswer, chainAnswer] = await Promise.all([
      members.has("sources")
        ? readSources().then(
            (observation) => ({ kind: "success" as const, observation }),
            () => ({ kind: "failed" as const }),
          )
        : Promise.resolve({ kind: "skipped" as const }),
      members.has("chain")
        ? modelsApi.getAgentChain(selectionBackend, modelId).then(
            (value) => ({ kind: "success" as const, value }),
            (error) =>
              apiFailure(error)?.code === "direct_mode"
                ? { kind: "direct" as const }
                : { kind: "failed" as const },
          )
        : Promise.resolve({ kind: "skipped" as const }),
    ]);
    if (token !== generation.current) return;
    const sourceCurrent =
      sourceAnswer.kind === "skipped"
        ? unknownSourceCurrent
        : sourceAnswer.kind === "success";
    setUnknownSourceCurrent(sourceCurrent);
    if (sourceAnswer.kind === "success") sourceAnswer.observation.install();

    if (chainAnswer.kind === "direct") {
      try {
        const observation = await readAgents();
        if (token !== generation.current) return;
        observation.install();
        const observedAgent = observation.value.find(
          (row) => row.backend === selectionBackend,
        );
        onDirectMode?.(
          {
            backend: selectionBackend,
            modelId,
            stage: submittedStage,
            submitted,
          },
          observedAgent ?? null,
        );
      } catch {
        if (token !== generation.current) return;
        setFailedMembers(new Set(["agents"]));
        setReconcileFailed(true);
        setPhase("unknown");
        focusAfterRender(retryButtonRef);
      }
      return;
    }

    if (chainAnswer.kind === "success") {
      onObserved?.(chainAnswer.value);
      setChain(chainAnswer.value);
      const observedHops = chainAnswer.value.chain.map(
        ({ source_id, model_id }) => ({ source_id, model_id }),
      );
      if (sameRouteDraft(observedHops, submitted)) {
        beginCommitted(
          {
            chain: chainAnswer.value,
            removed_hops: null,
            interrupted: null,
          },
          observedHops,
        );
        return;
      }
      setUnknownObservation("nonmatching");
    } else if (chainAnswer.kind === "failed") {
      setUnknownObservation("read_failed");
    }

    const failed = new Set<ReconcileMember>();
    if (!sourceCurrent) failed.add("sources");
    if (chainAnswer.kind === "failed") failed.add("chain");
    setFailedMembers(failed);
    setReconcileFailed(failed.size > 0);
    setPhase("unknown");
    focusAfterRender(retryButtonRef);
  };

  const retryUnknown = async () => {
    if (!selectionBackend || !submitted || phase !== "unknown") return;
    const token = ++generation.current;
    cancelButtonRef.current?.focus();
    setPhase("reconciling");
    setReconcileFailed(false);

    const retrySourceOnly =
      failedMembers.size === 1 && failedMembers.has("sources");
    const retryChainOnly =
      (failedMembers.size === 1 && failedMembers.has("chain")) ||
      (failedMembers.size === 0 &&
        unknownObservation === "nonmatching" &&
        unknownSourceCurrent);
    if (retrySourceOnly) {
      await reconcileUnknownMembers(token, new Set(["sources"]));
      return;
    }
    if (retryChainOnly) {
      await reconcileUnknownMembers(token, new Set(["chain"]));
      return;
    }
    if (failedMembers.size > 0 && !failedMembers.has("agents")) {
      await reconcileUnknownMembers(token, failedMembers);
      return;
    }

    try {
      const observation = await readAgents();
      if (token !== generation.current) return;
      observation.install();
      const observedAgent = observation.value.find(
        (row) => row.backend === selectionBackend,
      );
      if (!observedAgent) throw new Error("route_agent_missing");
      if (observedAgent.mode === "direct") {
        onDirectMode?.(
          {
            backend: selectionBackend,
            modelId,
            stage: submittedStage,
            submitted,
          },
          observedAgent,
        );
        return;
      }
      await reconcileUnknownMembers(token, new Set(["sources", "chain"]));
    } catch {
      if (token !== generation.current) return;
      setFailedMembers(new Set(["agents"]));
      setReconcileFailed(true);
      setPhase("unknown");
      focusAfterRender(retryButtonRef);
    }
  };

  const retryCommittedRead = () => {
    if (!report || phase !== "impact" || !commitReconciliation?.failed) return;
    doneButtonRef.current?.focus();
    commitReconciliation.retry();
  };
  // Every ET-5d move — arrow keys, Home/End, pointer activation — arrives here as
  // the list's single active key, so activeness and selection cannot diverge.
  const chooseCandidate = (next: string) => {
    setCandidate(
      matchedRef.current.find((item) => candidateKey(item.hop) === next) ?? null,
    );
  };
  const closeSelector = () => {
    setSelectorOpen(false);
    setQuery("");
    setCandidate(null);
  };
  const addCandidate = () => {
    if (!candidate) return;
    const next = advanceInteraction({ type: "append", hop: candidate.hop });
    closeSelector();
    requestAnimationFrame(() => gripRefs.current[next.focusIndex]?.focus());
  };
  const renderHop = (hop: RouteHop, index: number) => {
    const chainLink = chain?.chain.find((entry) =>
      equalHopIdentity(entry, hop),
    );
    const missing = chainLink?.reason === "source_missing";
    const joined = sources.some((source) => hopBelongsToSource(hop, source.id));
    const displayedSource = missing
      ? `${t("settings.models.routeDialog.sourceMissing")} · ${hop.source_id}`
      : joined
        ? sourceName(sources, hop.source_id)
        : hop.source_id;
    const chainCurrent = chain ? chain.current : null;
    const current = equalHopIdentity(chainCurrent, hop);
    return (
      <div
        key={`${hop.source_id}:${hop.model_id}:${index}`}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          if (grabbed !== null) {
            advanceInteraction({
              type: "move-grab-to",
              index,
            });
            advanceInteraction({ type: "drop-grab" });
          }
        }}
        data-current={current || undefined}
        className="model-hub-route-hop model-hub-fill-08 flex items-center border border-border"
      >
        <button
          ref={(node) => {
            gripRefs.current[index] = node;
          }}
          type="button"
          aria-label={t("settings.models.routeDialog.grip") as string}
          aria-grabbed={grabbed === index}
          onMouseDown={(event) => event.currentTarget.focus()}
          onClick={(event) => event.currentTarget.focus()}
          draggable={phase === "ready"}
          onDragStart={() => {
            advanceInteraction({ type: "begin-grab", index });
          }}
          onDragEnd={() => {
            advanceInteraction({ type: "drop-grab" });
          }}
          onFocus={() => advanceInteraction({ type: "focus", index })}
          onKeyDown={(event) => onGripKeyDown(event, index)}
          tabIndex={
            grabbed === index || (grabbed === null && rovingIndex === index)
              ? 0
              : -1
          }
          className="model-hub-route-grip grid shrink-0 place-items-center"
        >
          <GripVertical aria-hidden="true" />
        </button>
        <span
          className={cn(
            "model-hub-route-ordinal grid shrink-0 place-items-center font-mono font-medium",
            index === 0
              ? "model-hub-accent-pill--mint"
              : "model-hub-fill-0a text-muted",
          )}
        >
          {index + 1}
        </span>
        <span className="model-hub-route-hop-copy flex min-w-0 flex-1 flex-col">
          <span
            className="model-hub-route-hop-name truncate font-semibold text-foreground"
            title={displayedSource}
          >
            {missing ? (
              <>
                {t("settings.models.routeDialog.sourceMissing")}
                <span className="font-mono"> · {hop.source_id}</span>
              </>
            ) : !joined ? (
              <span className="font-mono">{hop.source_id}</span>
            ) : (
              sourceName(sources, hop.source_id)
            )}
          </span>
          <span
            className="model-hub-route-hop-model model-hub-ink-muted-b3 truncate font-mono"
            title={hop.model_id}
          >
            {hop.model_id}
          </span>
          {valid.invalidIndexes.includes(index) && (
            <span
              id={`model-hub-route-invalid-${index}`}
              className="model-hub-route-invalid"
            >
              {t("settings.models.routeDialog.invalidAfterRefresh")}
            </span>
          )}
        </span>
        <button
          ref={(node) => {
            removeRefs.current[index] = node;
          }}
          type="button"
          aria-label={t("settings.models.routeDialog.removeHop") as string}
          tabIndex={grabbed === null && rovingIndex === index ? 0 : -1}
          disabled={
            phase === "saving" || phase === "impact" || phase === "refreshing"
          }
          onClick={() => remove(index)}
          className="model-hub-route-remove model-hub-fill-0a grid shrink-0 place-items-center border border-border text-muted"
        >
          <X aria-hidden="true" />
        </button>
      </div>
    );
  };

  if (!selection) return null;
  const body =
    phase === "loading" ? (
      <div className="model-hub-route-body flex flex-1 items-center justify-center gap-2 text-muted">
        <LoaderCircle className="size-4 animate-spin" />
        {t("settings.models.routeDialog.loading")}
      </div>
    ) : phase === "unread" ? (
      <div className="model-hub-route-body flex flex-1 flex-col items-center justify-center gap-3 text-muted">
        <p>{t("settings.models.routeDialog.fail.read")}</p>
        <Button
          ref={retryButtonRef}
          type="button"
          onClick={() => {
            cancelButtonRef.current?.focus();
            void readChain();
          }}
        >
          {t("settings.models.routeDialog.retry")}
        </Button>
      </div>
    ) : phase === "direct" ? (
      <div className="model-hub-route-body flex flex-1 items-center justify-center text-muted">
        {t("settings.models.routeDialog.fail.read")}
      </div>
    ) : phase === "saving" ? (
      <div className="model-hub-route-body flex flex-1 items-center justify-center gap-2 text-muted">
        <LoaderCircle className="size-4 animate-spin" />
        <span ref={savingStatusRef} role="status" tabIndex={-1}>
          {t("settings.models.routeDialog.saving")}
        </span>
      </div>
    ) : phase === "guard" && guard ? (
      <div className="model-hub-route-body flex flex-1 flex-col gap-4">
        <div className="model-hub-guard-label">
          <p>{t("settings.models.guard.label")}</p>
          <span>{t("settings.models.guard.count", { count: guard.hops.length })}</span>
        </div>
        <ul className="model-hub-guard-list">
          {guard.hops.map((hop) => (
            <li
              key={`${hop.backend}:${hop.menu_model}:${hop.position}`}
              className="model-hub-guard-hop"
            >
              <span className="min-w-0 flex-1">
                <strong>
                  {t(`settings.models.backends.${hop.backend}`, {
                    defaultValue: hop.backend,
                  })} · {hop.menu_model}
                </strong>
                <span>
                  {hop.model_id} · {t("settings.models.guard.hop.position", {
                    n: hop.position,
                  })}
                </span>
              </span>
            </li>
          ))}
        </ul>
        <p className="model-hub-guard-hint text-destructive-ink">
          <Info aria-hidden="true" />
          {t("settings.models.guard.hint.interrupt")}
        </p>
      </div>
    ) : phase === "rejected" ? (
      <div className="model-hub-route-body flex flex-1 flex-col items-center justify-center gap-3 text-muted">
        <p>{t("settings.models.routeDialog.fail.save")}</p>
        <Button
          ref={retryButtonRef}
          type="button"
          disabled={!valid.valid}
          onClick={() => {
            generation.current += 1;
            void submit();
          }}
        >
          {t("settings.models.routeDialog.retry")}
        </Button>
      </div>
    ) : phase === "unknown" || phase === "reconciling" ? (
      <div className="model-hub-route-body flex flex-1 flex-col items-center justify-center gap-3 text-muted">
        <p>
          {t(
            reconcileFailed
              ? "settings.models.routeDialog.fail.reconcileRead"
              : "settings.models.routeDialog.fail.unconfirmed",
          )}
        </p>
        <Button
          ref={retryButtonRef}
          type="button"
          disabled={phase === "reconciling"}
          onClick={() => void retryUnknown()}
        >
          {t("settings.models.routeDialog.retry")}
        </Button>
      </div>
    ) : phase === "impact" || phase === "refreshing" ? (
      <div className="model-hub-route-body flex flex-1 flex-col gap-4">
        <h3 className="model-hub-route-label font-bold">
          {t("settings.models.routeDialog.impact.title")}
        </h3>
        {report?.removed_hops?.length ? (
          <ul className="model-hub-guard-list">
            {report.removed_hops.map((hop) => (
              <li
                key={`${hop.backend}:${hop.menu_model}:${hop.position}:${hop.source_id}:${hop.model_id}`}
                className="model-hub-guard-hop"
              >
                <span className="min-w-0 flex-1">
                  <strong>
                    {t(`settings.models.backends.${hop.backend}`, {
                      defaultValue: hop.backend,
                    })} · {hop.menu_model}
                  </strong>
                  <span>
                    {hop.model_id} · {t("settings.models.guard.hop.position", {
                      n: hop.position,
                    })}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        ) : null}
        <GuardGapList gaps={report?.interrupted ?? []} />
        {Boolean(report?.removed_hops?.length || report?.interrupted?.length) && (
          <p className="text-xs text-muted">
            {t("settings.models.routeDialog.impact.detail")}
          </p>
        )}
        {phase === "refreshing" && (
          <p className="text-xs text-muted">
            {t("settings.models.routeDialog.refreshing")}
          </p>
        )}
        {reconcileFailed && (
          <p className="text-xs text-muted">
            {t("settings.models.routeDialog.impact.refreshFail")}
          </p>
        )}
        {reconcileFailed && commitReconciliation?.failed && (
          <Button
            ref={retryButtonRef}
            type="button"
            disabled={phase === "refreshing"}
            onClick={() => void retryCommittedRead()}
          >
            {t("settings.models.routeDialog.retry")}
          </Button>
        )}
      </div>
    ) : (
      <div className="model-hub-route-body flex flex-col">
        <h3 className="model-hub-route-label font-bold">
          {t("settings.models.routeDialog.section")}
        </h3>
        <div className="model-hub-route-list flex flex-col border border-border bg-background">
          {draft.length ? (
            draft.map(renderHop)
          ) : (
            <div className="model-hub-route-empty text-xs text-muted">
              {t("settings.models.routeDialog.empty")}
            </div>
          )}
          {/* `modal` is what makes the panel scrollable: the Dialog's overlay owns a
              `react-remove-scroll` lock whose shards cover the dialog only, so a
              body-portalled non-modal popover has its wheel events cancelled. A
              modal popover pushes its own lock, which the dialog's defers to. */}
          <Popover
            modal
            open={selectorOpen}
            onOpenChange={(open) => {
              if (open) {
                setSelectorOpen(true);
                return;
              }
              closeSelector();
              focusAfterSelectorClose();
            }}
          >
            <PopoverTrigger asChild>
              <button
                ref={addButtonRef}
                type="button"
                disabled={candidates.length === 0}
                aria-describedby={
                  candidates.length === 0
                    ? "model-hub-route-add-none"
                    : undefined
                }
                className="model-hub-route-add model-hub-fill-05 flex w-full items-center justify-center gap-1.5 border border-border font-semibold text-muted disabled:opacity-60"
              >
                <Plus aria-hidden="true" />
                {t("settings.models.routeDialog.addHop")}
              </button>
            </PopoverTrigger>
            {/* Placement is deterministic and the height adapts, rather than the
                other way round. Two variants were measured and rejected: bounding
                collisions to the dialog gives a 131px panel over an 18px list,
                because an empty chain — the case where this picker matters most —
                makes the dialog short; letting it flip gives a full 300px panel
                that jumps 150px above the dialog's own top, covering the page
                title, since the room below happens to fall a few px short of the
                300px preference. Dropping down always and taking `min(300px,
                available-height)` keeps the panel attached to its trigger and on
                screen. It stays scrollable when a long chain leaves little room
                below; the dialog body scrolls, so the trigger can be moved up. */}
            <PopoverContent
              side="bottom"
              avoidCollisions={false}
              align="start"
              sideOffset={6}
              collisionPadding={16}
              className="model-hub-route-selector flex w-[var(--radix-popover-trigger-width)] max-w-[calc(100vw-64px)] flex-col p-0"
              onOpenAutoFocus={(event) => {
                event.preventDefault();
                searchRef.current?.focus();
              }}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                addButtonRef.current?.focus();
              }}
            >
              <Command
                shouldFilter={false}
                disablePointerSelection
                label={t("settings.models.routeDialog.addHop") as string}
                value={candidate ? candidateKey(candidate.hop) : ""}
                onValueChange={chooseCandidate}
                className="model-hub-route-selector-command min-h-0 bg-transparent"
              >
                <CommandInput
                  ref={searchRef}
                  value={query}
                  onValueChange={setQuery}
                  placeholder={
                    t("settings.models.routeDialog.add.search") as string
                  }
                />
                <p
                  className="model-hub-route-selector-head model-hub-route-selector-row"
                  aria-hidden="true"
                >
                  <span>{t("settings.models.routeDialog.add.source")}</span>
                  <span>{t("settings.models.routeDialog.add.model")}</span>
                </p>
                <CommandList className="model-hub-route-selector-list">
                  {matched.length === 0 && (
                    <CommandEmpty>
                      {t("settings.models.routeDialog.add.noMatch")}
                    </CommandEmpty>
                  )}
                  {candidateGroups.map((group) => (
                    <CommandGroup
                      key={group.source.id}
                      heading={group.source.display_name}
                      className="model-hub-route-selector-group [&_[cmdk-group-heading]]:sr-only"
                    >
                      {group.items.map((item, itemIndex) => (
                        <CommandItem
                          key={candidateKey(item.hop)}
                          value={candidateKey(item.hop)}
                          onSelect={() => setCandidate(item)}
                          className="model-hub-route-candidate model-hub-route-selector-row text-foreground"
                        >
                          {/* The frame prints the source once per group and leaves
                              the rest of the column blank; the group's own heading
                              carries it for assistive tech. */}
                          <span className="model-hub-route-candidate-source truncate">
                            {itemIndex === 0 ? group.source.display_name : ""}
                          </span>
                          <span className="model-hub-route-candidate-model truncate font-mono">
                            {item.hop.model_id}
                          </span>
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  ))}
                </CommandList>
                <div className="model-hub-route-selector-foot flex shrink-0 items-center justify-end border-t border-border">
                  <Button
                    type="button"
                    className="model-hub-route-selector-confirm px-4"
                    disabled={!candidate}
                    onClick={addCandidate}
                  >
                    {t("settings.models.routeDialog.add.confirm")}
                  </Button>
                </div>
              </Command>
            </PopoverContent>
          </Popover>
        </div>
        <button
          ref={reseedButtonRef}
          type="button"
          onClick={() => {
            const sorted = reorderRouteDraft(agent as AgentSupply, draft);
            advanceInteraction({ type: "reset", draft: sorted });
            announce(
              sameRouteDraft(sorted, draft)
                ? "settings.models.routeDialog.reorder.unchanged"
                : "settings.models.routeDialog.reorder.sorted",
            );
          }}
          className="model-hub-route-reseed flex items-center gap-1.5 self-start font-semibold text-cyan-ink"
        >
          <ListOrdered aria-hidden="true" />
          {t("settings.models.routeDialog.reorder.label")}
        </button>
        {candidates.length === 0 && (
          <span className="sr-only" id="model-hub-route-add-none">
            {t("settings.models.routeDialog.add.none")}
          </span>
        )}
        <p className="model-hub-route-hint flex items-start">
          <Info aria-hidden="true" className="shrink-0" />
          <span className="model-hub-route-hint-copy">
            {t("settings.models.routeDialog.hint")}
          </span>
        </p>
        {valid.invalidIndexes.length > 0 && (
          <p
            id="model-hub-route-invalid-summary"
            className="model-hub-route-invalid-summary"
          >
            {t("settings.models.routeDialog.invalidAfterRefresh")}
          </p>
        )}
        <span className="sr-only" aria-live="polite">
          {announcement ? t(announcement.key, announcement.params) : ""}
        </span>
      </div>
    );

  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && close()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-route-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          aria-busy={
            phase === "saving" ||
            phase === "refreshing" ||
            phase === "reconciling"
          }
          className="model-hub-route-dialog fixed left-1/2 z-50 flex -translate-x-1/2 flex-col gap-0 overflow-hidden border border-border-strong bg-surface p-0"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            cancelButtonRef.current?.focus();
          }}
        >
          <header className="model-hub-route-head flex flex-col border-b border-border">
            <span className="flex items-center justify-between gap-3">
              <DialogPrimitive.Title className="model-hub-route-title font-bold text-foreground">
                {t(
                  phase === "guard"
                    ? "settings.models.guard.title.saveRoute"
                    : "settings.models.routeDialog.title",
                  { menuModel: modelId },
                )}
              </DialogPrimitive.Title>
              <DialogPrimitive.Close
                disabled={phase === "saving"}
                aria-label={
                  t(
                    phase === "guard"
                      ? "settings.models.guard.cancel"
                      : phase === "impact" || phase === "refreshing"
                        ? "settings.models.routeDialog.impact.done"
                        : "settings.models.routeDialog.cancel",
                  ) as string
                }
                className="model-hub-route-close grid shrink-0 place-items-center"
              >
                <X aria-hidden="true" />
              </DialogPrimitive.Close>
            </span>
            <DialogPrimitive.Description className="model-hub-route-subtitle font-mono text-muted">
              {phase === "guard"
                ? t("settings.models.guard.subtitle.saveRoute")
                : backend}
            </DialogPrimitive.Description>
          </header>
          {body}
          <footer className="model-hub-route-foot model-hub-fill-05 flex items-center justify-end gap-2 border-t border-border">
            {phase === "impact" || phase === "refreshing" ? (
              <Button
                ref={doneButtonRef}
                type="button"
                className="model-hub-dialog-action"
                onClick={close}
              >
                {t("settings.models.routeDialog.impact.done")}
              </Button>
            ) : phase === "guard" && guard ? (
              <>
                <Button
                  ref={cancelButtonRef}
                  type="button"
                  variant="outline"
                  className="model-hub-dialog-action"
                  onClick={() => {
                    setGuard(null);
                    setPhase("ready");
                    requestAnimationFrame(() => saveButtonRef.current?.focus());
                  }}
                >
                  {t("settings.models.guard.cancel")}
                </Button>
                <Button
                  ref={saveButtonRef}
                  type="button"
                  className="model-hub-dialog-action"
                  onClick={() => void submit(guard)}
                >
                  {t("settings.models.guard.confirm.saveRoute")}
                </Button>
              </>
            ) : phase === "saving" ? (
              <span role="status" className="text-xs text-muted">
                {t("settings.models.routeDialog.saving")}
              </span>
            ) : (
              <>
                <Button
                  ref={cancelButtonRef}
                  type="button"
                  variant="outline"
                  className="model-hub-dialog-action"
                  onClick={close}
                >
                  {t("settings.models.routeDialog.cancel")}
                </Button>
                <Button
                  ref={saveButtonRef}
                  type="button"
                  className="model-hub-dialog-action"
                  aria-describedby={
                    valid.invalidIndexes.length > 0
                      ? [
                          "model-hub-route-invalid-summary",
                          ...valid.invalidIndexes.map(
                            (index) => `model-hub-route-invalid-${index}`,
                          ),
                        ].join(" ")
                      : undefined
                  }
                  disabled={phase !== "ready" || !dirty || !valid.valid}
                  onClick={() => void submit()}
                >
                  {t("settings.models.routeDialog.save")}
                </Button>
              </>
            )}
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};

export default RouteChainDialog;
