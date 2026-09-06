import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  GripVertical,
  Info,
  ListOrdered,
  LoaderCircle,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Undo2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { GuardGapList } from "./GuardGapList";
import { RouteCandidatePopover } from "./RouteCandidatePopover";
import { RouteOriginBadge } from "./RouteOriginBadge";
import { RouteRecordedTurn } from './RouteRecordedTurn';
import { eligibleSources } from "./eligibility";
import { equalHopIdentity, hopBelongsToSource } from "./hopIdentity";
import { apiFailure, modelsApi, type GuardConfirmation } from "./modelsApi";
import type { ModelChainRead } from "./modelRows";
import {
  routeCandidates,
  sameManualOverride,
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
  ManualRouteOverride,
  AgentChain,
} from "./types";

const sourceName = (sources: Source[], sourceId: string): string =>
  sources.find((source) => source.id === sourceId)?.display_name ?? sourceId;

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
  manual_override: ManualRouteOverride | null;
};

export const RouteChainDialog: React.FC<{
  selection: RouteChainSelection | null;
  sources: Source[];
  onClose: () => void;
  onCommitted?: (result: RouteReport) => void;
  commitReconciliation?: RouteCommitReconciliation | null;
  onObserved?: (chain: AgentChainMutation["chain"]) => void;
  onOpenDefaults?: () => void;
  covered?: boolean;
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
  onOpenDefaults,
  covered = false,
  readAgents,
  readSources,
  onDirectMode,
}) => {
  const { t } = useTranslation();
  const [phase, setPhase] = React.useState<Phase>("loading");
  const [origin, setOrigin] = React.useState<RouteHop[]>([]);
  const [savedOverride, setSavedOverride] = React.useState<ManualRouteOverride | null>(null);
  const [manualDraft, setManualDraft] = React.useState(false);
  const [preview, setPreview] = React.useState<AgentChain | null>(null);
  const [previewPending, setPreviewPending] = React.useState(false);
  const [previewFailed, setPreviewFailed] = React.useState(false);
  const previewGeneration = React.useRef(0);
  const restoreUndo = React.useRef<RouteHop[] | null>(null);
  const submittedOverride = React.useRef<ManualRouteOverride | null>(null);
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
  const [selectorEpoch, setSelectorEpoch] = React.useState(0);
  const [announcement, setAnnouncement] = React.useState<{
    key: string;
    params?: Record<string, unknown>;
  } | null>(null);
  const gripRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const editRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const removeRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
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
  React.useEffect(() => () => { generation.current += 1; previewGeneration.current += 1; }, []);

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
  const addCandidates = agent ? routeCandidates(agent, sources, draft) : [];
  const targetSources = agent ? eligibleSources(sources, agent) : [];
  const canEditRoute = draft.length > 0 || addCandidates.length > 0
    || targetSources.some((source) => source.kind === 'api_key');
  const backend = agent
    ? (t(`settings.models.backends.${agent.backend}`, {
        defaultValue: agent.backend,
      }) as string)
    : "";
  const valid = agent
    ? validateRouteDraft(agent, sources, origin, draft)
    : { invalidIndexes: [], valid: false };
  const dirty = manualDraft ? savedOverride === null || !sameRouteDraft(savedOverride.hops, draft) : savedOverride !== null;
  const draftOrigin = manualDraft ? (draft.length ? 'manual' : null) : (preview ?? chain)?.route_origin ?? null;
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
      setSavedOverride(next.manual_override);
      setManualDraft(next.manual_override !== null);
      setPreview(null);
      setPreviewFailed(false);
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
    previewGeneration.current += 1;
    restoreUndo.current = null;
    setPreview(null);
    setPreviewPending(false);
    setPreviewFailed(false);
    setGuard(null);
    setSubmitted(null);
    setSubmittedStage("initial");
    setReport(null);
    setReconcileFailed(false);
    setFailedMembers(new Set());
    setUnknownObservation("none");
    setUnknownSourceCurrent(false);
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
    setSelectorEpoch((current) => current + 1);
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

  const close = () => {
    if (phase === "guard") {
      setGuard(null);
      setPhase("ready");
      focusAfterRender(saveButtonRef);
      return;
    }
    if (phase !== "saving") {
      previewGeneration.current += 1;
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
      } else if (addCandidates.length > 0) {
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
  const cancelGrab = () => {
    if (!interactionRef.current.grab) return;
    const next = advanceInteraction({ type: "cancel-grab" });
    requestAnimationFrame(() => gripRefs.current[next.focusIndex]?.focus());
    announce("settings.models.routeDialog.reorder.cancelled", {
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
      event.stopPropagation();
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
    setSavedOverride(nextReport.chain.manual_override);
    setManualDraft(nextReport.chain.manual_override !== null);
    setPreview(null);
    restoreUndo.current = null;
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
    if (!selection || !agent || phase === "saving" || previewPending || (!manualDraft && previewFailed) || (manualDraft && !valid.valid) || !dirty)
      return;
    const hops = (confirmation ? (submitted ?? draft) : draft).map((hop) => ({
      ...hop,
    }));
    setSubmitted(hops);
    if (!confirmation) submittedOverride.current = manualDraft ? { hops } : null;
    setSubmittedStage(confirmation ? "confirmed" : "initial");
    setPhase("saving");
    setGuard(null);
    requestAnimationFrame(() => savingStatusRef.current?.focus());
    try {
      const guardFields = confirmation
          ? {
              force: true as const,
              would_remove_hops: confirmation.hops,
              would_interrupt: confirmation.gaps,
            }
          : {};
      const result = submittedOverride.current === null
        ? await modelsApi.restoreAgentChain(agent.backend, modelId, confirmation ? guardFields as GuardConfirmation : undefined)
        : await modelsApi.putAgentChain(agent.backend, modelId, { hops, ...guardFields });
      beginCommitted(
        {
          chain: result.chain,
          removed_hops: result.removed_hops,
          interrupted: result.interrupted,
        },
        result.chain.chain.map(({ source_id, model_id }) => ({ source_id, model_id })),
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
            manual_override: submittedOverride.current,
          },
          null,
        );
        return;
      }
      if (
        (failure?.code === "source_last_supplier" || failure?.code === 'source_in_route_chain') &&
        (failure.wouldInterrupt.length > 0 || failure.wouldRemoveHops.length > 0)
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
            manual_override: submittedOverride.current,
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
      if (sameManualOverride(chainAnswer.value.manual_override, submittedOverride.current)) {
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
            manual_override: submittedOverride.current,
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
  const addCandidate = (candidate: RouteCandidate) => {
    const next = advanceInteraction({ type: "append", hop: candidate.hop });
    requestAnimationFrame(() => gripRefs.current[next.focusIndex]?.focus());
  };
  const restoreAutomatic = React.useCallback(async () => {
    if (!agent || previewPending || phase !== 'ready') return;
    const token = ++previewGeneration.current;
    const previousDraft = draft.map((hop) => ({ ...hop }));
    setPreviewPending(true);
    setPreviewFailed(false);
    try {
      const next = await modelsApi.previewAgentChain(agent.backend, modelId, { manual_override: null });
      if (token !== previewGeneration.current) return;
      if (manualDraft) restoreUndo.current = previousDraft;
      setPreview(next);
      setManualDraft(false);
      advanceInteraction({ type: 'reset', draft: next.chain.map(({ source_id, model_id }) => ({ source_id, model_id })) });
      setAnnouncement({ key: 'settings.models.routing.restorePending' });
    } catch {
      if (token === previewGeneration.current) setPreviewFailed(true);
    } finally {
      if (token === previewGeneration.current) setPreviewPending(false);
    }
  }, [advanceInteraction, agent, draft, manualDraft, modelId, phase, previewPending]);
  const wasCovered = React.useRef(covered);
  React.useEffect(() => {
    const returning = wasCovered.current && !covered;
    wasCovered.current = covered;
    if (!returning || manualDraft) return;
    // Defaults can change while this dialog is hidden; explicit drafts remain local.
    if (restoreUndo.current) void restoreAutomatic();
    else void readChain();
  }, [covered, manualDraft, readChain, restoreAutomatic]);
  const undoRestore = () => {
    previewGeneration.current += 1;
    setPreview(null);
    setPreviewFailed(false);
    setManualDraft(true);
    advanceInteraction({ type: 'reset', draft: restoreUndo.current ?? origin });
    restoreUndo.current = null;
    announce('settings.models.routing.undoDone');
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
    const replacementCandidates = agent
      ? routeCandidates(
          agent,
          sources,
          draft.filter((_, draftIndex) => draftIndex !== index),
        )
      : [];
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
            className="model-hub-route-hop-name font-semibold text-foreground"
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
            className="model-hub-route-hop-model model-hub-ink-muted-b3 font-mono"
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
        <span className="model-hub-route-actions flex shrink-0 items-center">
          <RouteCandidatePopover
            key={`edit:${selectorEpoch}:${index}`}
            candidates={replacementCandidates}
            sources={targetSources}
            confirmLabel={t("settings.models.routeDialog.edit.confirm") as string}
            initialHop={hop}
            label={t("settings.models.routeDialog.editHop") as string}
            width="route"
            onReturnFocus={() => editRefs.current[index]?.focus()}
            onApply={(nextCandidate) => {
              const next = advanceInteraction({
                type: "replace",
                index,
                hop: nextCandidate.hop,
              });
              announce("settings.models.routeDialog.edit.replaced", {
                position: index + 1,
              });
              requestAnimationFrame(() =>
                gripRefs.current[next.focusIndex]?.focus(),
              );
            }}
            trigger={
              <button
                ref={(node) => {
                  editRefs.current[index] = node;
                }}
                type="button"
                aria-label={t("settings.models.routeDialog.editHop") as string}
                title={t("settings.models.routeDialog.editHop") as string}
                tabIndex={grabbed === null && rovingIndex === index ? 0 : -1}
                disabled={
                  phase === "saving" ||
                  phase === "impact" ||
                  phase === "refreshing"
                }
                className="model-hub-route-action model-hub-route-edit model-hub-fill-0a grid shrink-0 place-items-center border border-border text-muted"
              >
                <Pencil aria-hidden="true" />
              </button>
            }
          />
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
            className="model-hub-route-action model-hub-route-remove model-hub-fill-0a grid shrink-0 place-items-center border border-border text-muted"
          >
            <X aria-hidden="true" />
          </button>
        </span>
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
        <div className="model-hub-route-origin-line">
          <RouteOriginBadge origin={draftOrigin} backend={agent!.backend} interactive={false} />
          <span>{t(manualDraft ? 'settings.models.routing.frozen' : 'settings.models.routing.follows', { backend })}</span>
        </div>
        {preview && <div className="model-hub-route-preview" data-origin={preview.route_origin ?? 'unconfigured'} role="status">
          <strong>{t(`settings.models.routing.preview.${preview.route_origin ?? 'unconfigured'}`)}</strong>
          <span>{t('settings.models.routing.restorePending')}</span>
        </div>}
        <h3 className="model-hub-route-label font-bold">
          {t(preview ? 'settings.models.routing.afterSave' : "settings.models.routeDialog.section")}
        </h3>
        <div inert={previewPending} className="model-hub-route-list flex flex-col border border-border bg-background">
          {draft.length ? (
            manualDraft ? draft.map(renderHop) : draft.map((hop, index) => (
              <div key={`${hop.source_id}:${hop.model_id}`} className="model-hub-route-hop model-hub-fill-08 flex items-center border border-border">
                <span className="model-hub-route-ordinal grid shrink-0 place-items-center font-mono" data-origin={draftOrigin ?? 'unconfigured'}>{index + 1}</span>
                <span className="model-hub-route-hop-copy flex min-w-0 flex-1 flex-col">
                  <span className="model-hub-route-hop-name font-semibold" title={sourceName(sources, hop.source_id)}>{sourceName(sources, hop.source_id)}</span>
                  <span className="model-hub-route-hop-model font-mono text-muted" title={hop.model_id}>{hop.model_id}</span>
                </span>
              </div>
            ))
          ) : (
            <div className="model-hub-route-empty text-xs text-muted">
              <p>{t(manualDraft ? 'settings.models.routing.manualEmpty' : 'settings.models.routing.noDefaults')}</p>
              {!manualDraft && onOpenDefaults && <Button variant="outline" onClick={onOpenDefaults}><ListOrdered aria-hidden />{t('settings.models.routing.configureDefaults')}</Button>}
            </div>
          )}
          {manualDraft && <RouteCandidatePopover
            key={`add:${selectorEpoch}`}
            candidates={addCandidates}
            sources={targetSources}
            confirmLabel={t("settings.models.routeDialog.add.confirm") as string}
            label={t("settings.models.routeDialog.addHop") as string}
            width="trigger"
            onApply={addCandidate}
            onReturnFocus={() => addButtonRef.current?.focus()}
            trigger={
              <button
                ref={addButtonRef}
                type="button"
                disabled={targetSources.length === 0 || previewPending}
                aria-describedby={
                  addCandidates.length === 0
                    ? "model-hub-route-add-none"
                    : undefined
                }
                className="model-hub-route-add model-hub-fill-05 flex w-full items-center justify-center gap-1.5 border border-border font-semibold text-muted disabled:opacity-60"
              >
                <Plus aria-hidden="true" />
                {t("settings.models.routeDialog.addHop")}
              </button>
            }
          />}
        </div>
        {onOpenDefaults && <button type="button" disabled={previewPending} onClick={onOpenDefaults} className="model-hub-route-reseed flex items-center gap-1.5 self-start font-semibold text-cyan-ink">
          <ListOrdered aria-hidden="true" />
          {t('settings.models.routing.defaultRouting')}
          <span className="text-muted">{(agent?.sources?.order ?? []).map((id) => sourceName(sources, id)).join(' → ')}</span>
        </button>}
        {addCandidates.length === 0 && (
          <span className="sr-only" id="model-hub-route-add-none">
            {t("settings.models.routeDialog.add.none")}
          </span>
        )}
        <p className="model-hub-route-hint flex items-start">
          <Info aria-hidden="true" className="shrink-0" />
          <span className="model-hub-route-hint-copy">
            {t(manualDraft ? 'settings.models.routing.manualHint' : 'settings.models.routing.editHint')}
          </span>
        </p>
        {previewFailed && <div role="alert" className="text-destructive-ink text-xs"><p>{t('settings.models.routing.previewFailed')}</p><Button variant="ghost" size="sm" disabled={previewPending} onClick={() => void restoreAutomatic()}><RefreshCw aria-hidden />{t('settings.models.routeDialog.retry')}</Button></div>}
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
    <DialogPrimitive.Root open={!covered} onOpenChange={(open) => !open && close()}>
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
          onEscapeKeyDown={(event) => {
            if (phase === "ready" && interactionRef.current.grab) {
              event.preventDefault();
              cancelGrab();
            }
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
          <RouteRecordedTurn key={`${selectionBackend}:${modelId}`} backend={selectionBackend!} modelId={modelId} />
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
                {(manualDraft || restoreUndo.current) && <Button ref={reseedButtonRef} variant="outline" className="model-hub-dialog-action mr-auto" disabled={phase !== 'ready' || previewPending} onClick={() => restoreUndo.current ? undoRestore() : void restoreAutomatic()}>
                  {previewPending ? <LoaderCircle className="animate-spin" aria-hidden /> : restoreUndo.current ? <Undo2 aria-hidden /> : <RotateCcw aria-hidden />}
                  {t(restoreUndo.current ? 'settings.models.routing.undoRestore' : 'settings.models.routing.restoreAutomatic')}
                </Button>}
                <Button
                  ref={cancelButtonRef}
                  type="button"
                  variant="outline"
                  className="model-hub-dialog-action"
                  onClick={close}
                >
                  {t(manualDraft || preview ? "settings.models.routeDialog.cancel" : 'settings.models.routing.close')}
                </Button>
                {!manualDraft && !preview ? (canEditRoute && <Button variant="outline" className="model-hub-dialog-action" disabled={phase !== 'ready'} onClick={() => setManualDraft(true)}><Pencil aria-hidden />{t('settings.models.routing.editRoute')}</Button>) : <Button
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
                  disabled={phase !== "ready" || previewPending || (!manualDraft && previewFailed) || !dirty || (manualDraft && !valid.valid)}
                  onClick={() => void submit()}
                >
                  {t("settings.models.routeDialog.save")}
                </Button>}
              </>
            )}
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};

export default RouteChainDialog;
