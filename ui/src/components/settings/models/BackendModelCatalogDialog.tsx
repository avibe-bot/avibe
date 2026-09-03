// One model-catalog dialog for every backend.
//
// It replaces OpenCodeMenuDialog, which existed because OpenCode was the only
// backend whose menu the user could edit. That distinction was never a product
// idea — it was the shape of the old data. With one catalog per backend, one
// dialog edits it, and Claude Code's `Default` is not a special case in the UI
// but an ordinary row the server marked `locked`.
//
// It shows the catalog and nothing else: no Source, no Route, no fallback, no
// mapping. Which supplier serves a model is a Route question, and the Route rows
// on the Agent card are where it stays answered.
import * as React from 'react';
import { Reorder, useDragControls } from 'framer-motion';
import { GripVertical, Lock, LoaderCircle, Pencil, Plus, Search, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import type { PendingWrite } from './asyncLifetime';
import {
  applyBackendCatalogIntent,
  backendCatalogIntent,
  backendCatalogIntentApplied,
  backendModelId,
  catalogModelIds,
  chosenCandidate,
  draftRowFor,
  echoableRefusal,
  heldRowFor,
  offeredCandidates,
  orderWithRestored,
  readBackendCatalogBaseline,
  sameCatalog,
  samePlanContents,
  type BackendCatalogBaseline,
  type BackendCatalogIntent,
  type ChosenCandidate,
} from './backendCatalog';
import { BackendModelEditorDialog } from './BackendModelEditorDialog';
import { BackendModelPickerDialog } from './BackendModelPickerDialog';
import { GuardImpact, type GuardPicture, type GuardPlan } from './GuardImpact';
import { apiFailure, modelsApi } from './modelsApi';
import { movedOrder, sameIds } from './reorder';
import { catalogSaveFailureKey, catalogSaveLeftUnloaded } from './serverCopy';
import type {
  AgentBackend,
  AgentSupply,
  BackendModel,
  BackendModelsPut,
  ModelCandidate,
  RouteHopRef,
  SupplyGap,
} from './types';

type ReadState = 'loading' | 'ready' | 'error';

/** Same shape the Source order drawer announces with: the key alone would tell a
 *  screen-reader user something moved without telling them where to. */
type AnnouncementKey = 'grabbed' | 'moved' | 'dropped' | 'grabCancelled';

type Announcement = {
  key: AnnouncementKey;
  model: string;
  position: number;
  count: number;
} | null;

const matchesQuery = (model: BackendModel, query: string, renderedLabel: string): boolean => {
  if (query === '') return true;
  const needle = query.toLowerCase();
  return model.id.toLowerCase().includes(needle)
    || renderedLabel.toLowerCase().includes(needle);
};

/**
 * One removal, waiting on an answer, carrying the account of its consequence
 * the user is being shown.
 *
 * The plan travels with the question because a re-ask after the guard refused
 * is about the server's plan and a first ask is about the baseline's — the
 * question is not answerable without the plan it was raised from. `account`
 * names whose plan it is, and that is the whole reason the field exists:
 * agreeing to what the dialog projected from the routes it holds is not
 * agreeing to what the server refused. Only a `guard` answer can settle a
 * refusal, so only a `guard` answer is allowed to discharge what one owes.
 */
type RemovalQuestion = { modelId: string; plan: GuardPicture; account: 'draft' | 'guard' };

/** A guard refusal, as received, together with the write it refused. Both
 *  halves are needed to use it: the arrays are what a retry echoes, and the
 *  write is what makes them true — the same refusal says nothing about a
 *  different list. */
type StoredRefusal = {
  hops: readonly RouteHopRef[];
  gaps: readonly SupplyGap[];
  baseline: readonly BackendModel[];
  models: readonly BackendModel[];
  /**
   * The held-back removals still owed an answer against the server's own plan.
   *
   * This is the acceptance bit: empty means accepted, and only an accepted
   * refusal may be echoed with `force`. It is separate from the write above
   * because the two answer different questions — the write says 「is this the
   * same save the server refused?」 and this says 「did the user accept that
   * refusal?」 — and the reachable path that made one stand in for the other is
   * why it exists. A queued question can be taken off the screen by removing
   * its row through the ordinary trash action, which recomputes from the
   * baseline and shows the dialog's own projection instead; answer the rest and
   * the draft is byte-for-byte the refused list again, so equality alone would
   * have forced a plan whose interruptions were never displayed.
   *
   * Discharged one id at a time, and only by a `guard` answer. A swallowed
   * question therefore leaves the save unforced, the server refuses it again
   * with the same arrays, and the question comes back — one extra round-trip,
   * and no loop, because acceptance is the only route to `force`.
   */
  owed: ReadonlySet<string>;
};

/**
 * The guard's refusal, split into the questions it forces.
 *
 * The server reports one plan for the whole write and the confirmation lives
 * inside a row, so each held-back removal is asked with the part of that plan
 * that names it: hops by the menu model they serve, gaps by the model they
 * would strand. Splitting decides what to SHOW and nothing else — what the next
 * save echoes is the refusal exactly as it arrived, never these pieces
 * reassembled (C3) — so a split that cannot cover every element still asks
 * everything it can rather than editing the plan down to what it understood.
 *
 * Anything the refusal names that this write does not remove belongs to no
 * question this dialog can ask, so it produces none; with nothing to ask at
 * all, the caller falls through to the failure sentence instead of retrying
 * forever.
 */
const refusalPlans = (
  hops: readonly RouteHopRef[],
  gaps: readonly SupplyGap[],
  removed: ReadonlySet<string>,
  backend: AgentBackend,
): RemovalQuestion[] => {
  const plans = new Map<string, GuardPlan>();
  const planFor = (modelId: string): GuardPlan | null => {
    if (!removed.has(modelId)) return null;
    const existing = plans.get(modelId);
    if (existing) return existing;
    const created: GuardPlan = { hops: [], gaps: [] };
    plans.set(modelId, created);
    return created;
  };
  for (const hop of hops) planFor(hop.menu_model)?.hops.push(hop);
  for (const gap of gaps) {
    if (gap.backend === backend) planFor(gap.model_id)?.gaps.push(gap);
  }
  return [...plans].map(([modelId, plan]) => ({ modelId, plan, account: 'guard' as const }));
};

/** The stale-candidate refusal (C1), named by the route's own `error` rather
 *  than by status: nothing was committed and nothing was interrupted, so it is
 *  answered by asking again with today's suppliers, not by a failure sentence. */
const CANDIDATES_CHANGED = 'candidate_suppliers_changed';

/** The route guard (C3). Like the refusal above it commits nothing, so it is
 *  answered by asking again with the plan the server actually has — not by a
 *  sentence about a save that never happened. */
const MODEL_IN_ROUTE = 'backend_model_in_route';

/** The ids a picker may not offer: everything the draft holds, less the ones it
 *  was reopened to ask about. A re-ask is about a row the draft still holds (the
 *  addition was refused, not applied), so counting it as 「already in the list」
 *  would show the user their own pending addition as an unpickable row and leave
 *  the question it was reopened to ask unanswerable. */
const offerable = (taken: ReadonlySet<string>, seed: ReadonlySet<string>): ReadonlySet<string> => {
  if (seed.size === 0) return taken;
  const listed = new Set(taken);
  for (const id of seed) listed.delete(id);
  return listed;
};

/** Nothing pre-picked, held once so opening the picker is not a new object each
 *  render — the picker applies its seed when it opens. */
const NO_SEED: ReadonlySet<string> = new Set();

export const BackendModelCatalogDialog: React.FC<{
  open: boolean;
  backend: AgentBackend;
  /** Whether this role may read Sources. The candidates read names them, so the
   *  action that opens it is offered exactly where the page's other
   *  Source-reading surfaces are (C4). */
  canReadSources: boolean;
  /** Source id → name, for the removal confirmation's hops. The catalog names no
   *  Source of its own (that stays with the Route), but a removal that takes a
   *  Route with it has to say whose hop goes away — so the page's own Sources
   *  answer it, and an id they do not cover simply goes unnamed. */
  sourceNames: Readonly<Record<string, string>>;
  onClose: () => void;
  onSaved: (echoed: AgentSupply) => void | Promise<void>;
  onObserved: (observed: AgentSupply) => void | Promise<void>;
  catalogWrite: PendingWrite;
}> = ({ open, backend, canReadSources, sourceNames, onClose, onSaved, onObserved, catalogWrite }) => {
  const { t } = useTranslation();
  const [baseline, setBaseline] = React.useState<BackendCatalogBaseline | null>(null);
  const baselineRef = React.useRef<BackendCatalogBaseline | null>(null);
  const [readState, setReadState] = React.useState<ReadState>('loading');
  const [draft, setDraft] = React.useState<BackendModel[]>([]);
  const draftRef = React.useRef<BackendModel[]>([]);
  const [query, setQuery] = React.useState('');
  const [saveFailedKey, setSaveFailedKey] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState<{ model: BackendModel | null; seedId?: string } | null>(null);
  const [picking, setPicking] = React.useState<{ seed: ReadonlySet<string> } | null>(null);
  const [removing, setRemoving] = React.useState<RemovalQuestion | null>(null);
  const [grabbedId, setGrabbedId] = React.useState<string | null>(null);
  const [announcement, setAnnouncement] = React.useState<Announcement>(null);
  const readAttempt = React.useRef(0);
  const grips = React.useRef(new Map<string, HTMLButtonElement>());
  const grabbedFrom = React.useRef<string[] | null>(null);
  /**
   * The picks this draft holds an agreement for (C1), each still paired with the
   * suppliers its row displayed.
   *
   * One object per pick, and this is its only home: a set of added ids beside a
   * map of expectations is two records of one agreement, and the id can leave
   * the set while its promise stays behind — which is how a save comes to send
   * a projection nobody agreed to. Kept in a ref because it is not rendered;
   * the list on screen is the decision, and this is what it was made against.
   */
  const chosenRef = React.useRef(new Map<string, ChosenCandidate>());
  /**
   * Per removed id, the consequence the user was shown and accepted (C3).
   *
   * A record of what was asked, never a source of what is sent: the arrays a
   * forced write carries come from `refusalRef` and from nowhere else. This one
   * answers a different question — 「does the server's refusal say what the user
   * already agreed to?」 — and its only effect is whether the same consequence is
   * put to them a second time.
   */
  const previewRef = React.useRef(new Map<string, GuardPicture>());
  /**
   * The last guard refusal, stored exactly as it arrived and bound to the write
   * it refused.
   *
   * The single source of a forced body (C3). The two arrays are the server's
   * own, kept verbatim and in the server's order, because that is what the
   * server compares the echo against — a client that rebuilt them from what it
   * showed would be answering with its own sentence and be refused for the
   * wording, not the meaning.
   *
   * The write is stored with them so the echo cannot outlive its subject. A
   * refusal answers one exact `baseline` + `models` pair; the moment the draft
   * moves, the plan describes consequences of a write nobody is making any
   * more, and `putBody` finds it no longer matching rather than having to be
   * told to forget it.
   */
  const refusalRef = React.useRef<StoredRefusal | null>(null);
  /** Removals the guard held back, still owed an answer, each with the plan the
   *  server refused them for. The confirmation lives inside the row it is about,
   *  so they are asked one at a time — and all of them, because a row that came
   *  back unasked is a removal the user requested and nobody ever answered. */
  const guardedRef = React.useRef<RemovalQuestion[]>([]);

  /**
   * Ask the next held-back removal, or stop asking. Also how an ordinary
   * confirmation closes: with nothing held back, this is `setRemoving(null)`.
   *
   * A queued question whose row has since left the draft is discarded rather
   * than asked. The confirmation renders inside the row it is about, so a
   * question about a row that no longer exists — a fresh baseline the server no
   * longer holds it in, an addition withdrawn on the way here — has nothing to
   * remove and nowhere to appear: asking it would leave the dialog waiting on an
   * answer the user has no controls to give. Discarding it is not losing the
   * user's intent either; the row it named is already gone.
   */
  const askNextGuarded = () => {
    const held = new Set(draftRef.current.map((model) => model.id));
    let next = guardedRef.current.shift();
    while (next && !held.has(next.modelId)) next = guardedRef.current.shift();
    setRemoving(next ?? null);
  };

  const applyBaseline = React.useCallback((observed: BackendCatalogBaseline, models: BackendModel[]) => {
    baselineRef.current = observed;
    setBaseline(observed);
    draftRef.current = models;
    setDraft(models);
    setReadState('ready');
  }, []);

  const loadBaseline = React.useCallback((preserveDraft: boolean) => {
    const attempt = ++readAttempt.current;
    setReadState('loading');
    void (async () => {
      try {
        const observed = await readBackendCatalogBaseline(modelsApi, backend);
        if (attempt !== readAttempt.current) return;
        const server = observed.models ?? [];
        const previous = baselineRef.current?.models;
        const next = preserveDraft && previous
          ? applyBackendCatalogIntent(server, backendCatalogIntent(previous, draftRef.current))
          : server;
        applyBaseline(observed, next);
      } catch {
        if (attempt === readAttempt.current) setReadState('error');
      }
    })();
  }, [applyBaseline, backend]);

  React.useEffect(() => {
    if (!open) return;
    // All of these describe decisions taken against one baseline, so a dialog
    // that is reading a new one starts with none of them.
    chosenRef.current = new Map();
    previewRef.current = new Map();
    refusalRef.current = null;
    guardedRef.current = [];
    setPicking(null);
    setRemoving(null);
    loadBaseline(false);
    return () => { readAttempt.current += 1; };
  }, [loadBaseline, open]);

  const mutate = (next: BackendModel[]) => {
    draftRef.current = next;
    setDraft(next);
    setRemoving(null);
    setSaveFailedKey(null);
  };

  // The server sent no `catalog_models`, so this build is talking to a release
  // that predates the catalog. The legacy list is still worth showing — it is
  // what the backend really exposes — but the dialog will not offer to write a
  // baseline it assembled itself.
  const legacy = baseline !== null && baseline.models === null;
  const legacyIds = legacy && baseline ? catalogModelIds(baseline.agent) : [];
  const ready = readState === 'ready' && baseline !== null;
  const editable = ready && !legacy;
  const busy = catalogWrite.pending;
  const dirty = editable && !sameCatalog(baseline?.models ?? [], draft);
  /** A save that failed after the re-read rebased the draft leaves nothing to
   *  send and everything still to do — a catalog the backend never loaded reads
   *  as settled, so pressing Save again has to stay possible. */
  const retryable = editable && saveFailedKey !== null;
  const filtering = query.trim() !== '';
  const displayLabel = (model: BackendModel): string => (
    backend === 'claude' && model.id === 'default' && model.locked && !model.routeable
      ? t('settings.models.gateway.catalog.systemDefault') as string
      : model.display_name ?? model.id
  );
  /**
   * The rows on screen.
   *
   * A pending removal question is always one of them, whatever the query says.
   * Its confirmation renders inside its own row (the shape every guarded Model
   * Hub mutation asks in), so a filter that hides that row hides the only
   * controls that can answer it — and the draft has already restored the row, so
   * Save stays disabled with nothing on screen to explain why. Deriving
   * visibility from the queue rather than keeping the two beside each other is
   * what makes 「a question that is pending is on screen」 a property of this one
   * line instead of a rule every path that advances the queue has to remember.
   */
  const visible = draft.filter((model) => (
    model.id === removing?.modelId || matchesQuery(model, query.trim(), displayLabel(model))
  ));
  const movableIds = draft.filter((model) => !model.locked).map((model) => model.id);
  const takenIds = new Set(draft.map((model) => model.id));
  const effortSuggestions = [...new Set(draft.flatMap((model) => model.reasoning_efforts))];
  /** Threaded from the server's own projection rather than mirrored here: the
   *  editor's id rule has to agree with the backend that will accept the id. */
  const standardVendors = React.useMemo(
    () => new Set(baseline?.agent.standard_vendors ?? []),
    [baseline?.agent.standard_vendors],
  );

  /** Reordering permutes which movable row sits in which movable slot; a locked
   *  row keeps its absolute index, so the order sent never moves one. */
  const reorderMovable = (nextIds: string[]) => {
    const byId = new Map(draft.map((model) => [model.id, model]));
    let cursor = 0;
    mutate(draft.map((model) => (model.locked ? model : byId.get(nextIds[cursor++]) as BackendModel)));
  };

  const label = (modelId: string): string => {
    const model = draft.find((entry) => entry.id === modelId);
    return model ? displayLabel(model) : modelId;
  };

  const announce = (key: AnnouncementKey, modelId: string, order: string[] = movableIds) => {
    setAnnouncement({
      key,
      model: label(modelId),
      position: order.indexOf(modelId) + 1,
      count: order.length,
    });
  };

  const focusGrip = (modelId: string) => {
    requestAnimationFrame(() => grips.current.get(modelId)?.focus());
  };

  const cancelGrab = () => {
    const restored = grabbedFrom.current;
    grabbedFrom.current = null;
    const focused = grabbedId;
    setGrabbedId(null);
    if (restored && !sameIds(restored, movableIds)) reorderMovable(restored);
    if (focused) {
      announce('grabCancelled', focused, restored ?? movableIds);
      focusGrip(focused);
    }
  };

  const handleGripKey = (modelId: string, event: React.KeyboardEvent<HTMLButtonElement>) => {
    const index = movableIds.indexOf(modelId);
    if (index < 0) return;
    if (event.key === ' ') {
      event.preventDefault();
      if (grabbedId === modelId) {
        grabbedFrom.current = null;
        setGrabbedId(null);
        announce('dropped', modelId);
      } else {
        grabbedFrom.current = movableIds;
        setGrabbedId(modelId);
        announce('grabbed', modelId);
      }
      return;
    }
    if (event.key === 'Escape' && grabbedId === modelId) {
      event.preventDefault();
      event.stopPropagation();
      cancelGrab();
      return;
    }
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    const delta = event.key === 'ArrowUp' ? -1 : 1;
    if (grabbedId === modelId) {
      const next = movedOrder(movableIds, index, delta);
      if (sameIds(next, movableIds)) return;
      reorderMovable(next);
      announce('moved', modelId, next);
      focusGrip(modelId);
      return;
    }
    const neighbour = movableIds[index + delta];
    if (neighbour) focusGrip(neighbour);
  };

  /**
   * A preview of what a removal would take with it, from the routes this dialog
   * already holds.
   *
   * It exists so the question can be asked when the user clicks the trash,
   * rather than after a round-trip — but it is a picture, not a plan, and
   * nothing it produces is ever sent. Only the server states the consequences
   * of a write (C3); this reads the baseline's own hops in their one-based
   * positions so that the picture matches the statement, and when it does the
   * save goes through without asking the same question twice.
   *
   * The gaps are `null`, not empty: whether a removal strands an Agent is the
   * guard's answer, and this dialog holds no supply to answer it from. Empty
   * would have made `GuardImpact` read 「these models still have another source
   * available」 — a promise about the rest of the Hub, made by the one surface
   * that cannot see it, in the confirmation the user decides on. So the picture
   * shows its hops and says nothing about interruption; the guard refuses with
   * the real gaps before anything commits, and those are what the user is then
   * shown.
   */
  const removalPreview = (modelId: string): GuardPicture => ({
    hops: (baseline?.agent.routes?.[modelId]?.hops ?? []).map((hop, index) => ({
      ...hop,
      backend,
      menu_model: modelId,
      position: index + 1,
    })),
    gaps: null,
  });

  /** The row leaves the draft; its route leaves with it when the list saves. */
  const dropModel = (modelId: string, plan: GuardPicture) => {
    chosenRef.current.delete(modelId);
    if (plan.hops.length > 0 || (plan.gaps?.length ?? 0) > 0) previewRef.current.set(modelId, plan);
    else previewRef.current.delete(modelId);
    mutate(draftRef.current.filter((entry) => entry.id !== modelId));
  };

  /**
   * The user answers one removal question: the row leaves, and if the question
   * was the guard's own, the refusal is one answer closer to being echoable.
   *
   * Only a `guard` answer discharges anything. The same confirmation renders for
   * a removal the dialog itself asked about, and what that one shows is the
   * dialog's projection from the routes it holds — accepting it says nothing
   * about the plan the server refused, which may name interruptions the
   * projection had no way to know about.
   */
  const acceptRemoval = (question: RemovalQuestion) => {
    dropModel(question.modelId, question.plan);
    const refusal = refusalRef.current;
    if (question.account !== 'guard' || !refusal || !refusal.owed.has(question.modelId)) return;
    const owed = new Set(refusal.owed);
    owed.delete(question.modelId);
    refusalRef.current = { ...refusal, owed };
  };

  const removeModel = (model: BackendModel) => {
    const plan = removalPreview(model.id);
    // A route is the only thing a removal takes with it that the user did not
    // name, so it is the only removal that asks first.
    if (plan.hops.length > 0) {
      setRemoving({ modelId: model.id, plan, account: 'draft' });
      return;
    }
    dropModel(model.id, plan);
  };

  const commitEdit = (model: BackendModel) => {
    const existing = draft.findIndex((entry) => entry.id === model.id);
    // A row written by hand promises nothing about its suppliers, so an id
    // created here carries no displayed projection into the save. Editing an
    // existing row keeps whatever it already carried: the editor holds its id
    // fixed, so the projection is still about the same addition.
    if (existing < 0) chosenRef.current.delete(model.id);
    mutate(existing >= 0
      ? draft.map((entry, index) => (index === existing ? { ...model, locked: entry.locked, routeable: entry.routeable } : entry))
      : [...draft, model]);
    setEditing(null);
  };

  /**
   * Picks, poured into the draft.
   *
   * Each arrives as the agreement itself — the candidate and the suppliers its
   * row displayed — and is stored whole. The server matches the addition at
   * commit time, so this states which projection the user agreed to, and a
   * changed one is re-asked instead of silently seeding a route the picker never
   * displayed (C1). A pick the draft already holds is exactly that re-ask: the
   * refused save left the row alone and left only the agreement undone, so
   * confirming supplies it without rebuilding a row the user may have edited.
   *
   * A seeded id that comes back unpicked has no agreement any more, and this
   * dialog has no way to add a row without one — the picker is where a supplier
   * promise is made, and a pending addition that survived with none would be
   * sent as if it had been written by hand, which is precisely the silent
   * re-send the re-ask exists to prevent. So it leaves the draft with its
   * promise. Never a saved row: a disagreement about suppliers may not delete
   * something the server already holds, and it cannot — a seed only ever names
   * pending additions.
   *
   * 「Confirm none of them」 is therefore the same operation as 「dismiss the
   * re-ask」, and the picker's Cancel is wired to exactly this call with no picks:
   * a re-ask the user walks away from has answered every seeded id with 「not
   * this」, and leaving those rows behind is what let a refused projection be
   * re-sent by the next Save with no way to stop it. An ordinary add has an empty
   * seed, so the same call still means 「nothing happened」.
   *
   * Which ROW an id lands as is not decided here — `draftRowFor` owns that, so a
   * re-added row comes back as the one the user already has rather than as a
   * fresh synthesis of the proposal.
   */
  const addCandidates = (picked: ChosenCandidate[]) => {
    const seed = picking?.seed ?? NO_SEED;
    setPicking(null);
    const confirmed = new Set(picked.map((pick) => pick.candidate.id));
    const saved = new Set((baselineRef.current?.models ?? []).map((model) => model.id));
    const withdrawn = new Set([...seed].filter((id) => !confirmed.has(id) && !saved.has(id)));
    if (picked.length === 0 && withdrawn.size === 0) return;
    for (const id of withdrawn) chosenRef.current.delete(id);
    for (const pick of picked) {
      chosenRef.current.set(pick.candidate.id, pick);
      // Re-adding a row voids the removal that was confirmed for it.
      previewRef.current.delete(pick.candidate.id);
    }
    const held = new Set(draftRef.current.map((model) => model.id));
    const additions = picked
      .filter((pick) => !held.has(pick.candidate.id))
      .map((pick) => draftRowFor(pick.candidate, draftRef.current, baselineRef.current?.models ?? []));
    mutate([...draftRef.current.filter((model) => !withdrawn.has(model.id)), ...additions]);
  };

  /**
   * What the server offers right now, or `null` when that could not be read.
   *
   * A 409 answers a pick, a pick came from the picker, and the picker is only
   * offered where Sources are readable (C4) — so a read that does not arrive
   * here is a failed read, not a role forbidden to take one, and `null` is the
   * whole of what this dialog learned about the offer. The caller decides what
   * to do with not knowing; it may not guess.
   */
  const offeredNow = async (): Promise<Map<string, ModelCandidate> | null> => {
    try {
      return offeredCandidates(await modelsApi.getAgentModelCandidates(backend));
    } catch {
      return null;
    }
  };

  /**
   * The one write this dialog makes.
   *
   * `baseline` + `models` is the whole list settling through a single optimistic
   * merge (C1). The other three fields exist only because two of that merge's
   * consequences are not derivable from the list: `force` answers the route
   * guard, and `expected_suppliers` states, per addition, the projection the
   * picker displayed for it. Only per addition: a row the baseline already holds
   * is not matched again, so a promise about it would describe nothing this
   * write does.
   *
   * The forced arrays are produced HERE and only here, from the stored refusal
   * and only from it, verbatim and in the server's own order (C3). That is what
   * makes the echo an echo: the server compares it against what it sent, and
   * it compares element by element, so a plan reassembled from per-row pieces
   * would carry the order they were clicked in and be refused for saying the
   * same thing differently. Nothing this dialog composes can reach these two
   * fields — there is no path to them that does not pass through a refusal the
   * server wrote.
   *
   * Whether a stored refusal has earned that echo is `echoableRefusal`'s
   * question, asked against this write rather than against a flag someone has
   * to remember to clear.
   */
  const putBody = (baselineModels: BackendModel[], requested: BackendModel[]): BackendModelsPut => {
    const body: BackendModelsPut = { baseline: baselineModels, models: requested };
    const refusal = refusalRef.current;
    if (refusal && echoableRefusal(refusal, baselineModels, requested)) {
      body.force = true;
      body.would_remove_hops = [...refusal.hops];
      body.would_interrupt = [...refusal.gaps];
    }
    const baselineIds = new Set(baselineModels.map((model) => model.id));
    const expected = Object.fromEntries(
      requested.flatMap((model) => {
        const pick = baselineIds.has(model.id) ? undefined : chosenRef.current.get(model.id);
        return pick ? [[model.id, pick.expected_suppliers] as const] : [];
      }),
    );
    if (Object.keys(expected).length > 0) body.expected_suppliers = expected;
    return body;
  };

  const save = () => {
    const base = baselineRef.current;
    // A pre-catalog server has no baseline to send, so there is nothing this
    // branch could honestly PUT.
    if (!base || base.models === null) return;
    const baselineModels = base.models;
    const requested = draftRef.current;
    const intent = backendCatalogIntent(baselineModels, requested);
    setSaveFailedKey(null);
    const body = putBody(baselineModels, requested);
    void catalogWrite.track(async () => {
      let echoed: AgentSupply;
      try {
        echoed = await modelsApi.putAgentModels(backend, body);
      } catch (error) {
        const failure = apiFailure(error);
        if (failure?.code === CANDIDATES_CHANGED) {
          // Nothing was committed, so there is no list to re-read and nothing to
          // report: the answer is the same question again, and the draft is
          // still the user's. The rows stay exactly as they built them — a
          // context window they widened or a name they wrote is theirs, and
          // rebuilding the row from its candidate would spend those edits to
          // answer a question about suppliers. Only the projection is in doubt,
          // so only the projection waits: it stays as displayed until the re-ask
          // replaces it, which leaves the guard armed rather than granting an
          // agreement the user never gave if they dismiss the picker instead of
          // answering it.
          //
          // The refusal is reconciled HERE, where the evidence arrives, and
          // before anything reopens — so no later path has to remember to undo a
          // projection the server has already refused.
          //
          // What `changed` reports is suppliers, and suppliers are not the
          // question of whether an id is still on offer: a built-in the server
          // still serves with none of them is a legitimate candidate whose route
          // starts empty (C1), so reading an empty list as a withdrawal deletes
          // a row the user may still add. Only one thing withdraws an id — the
          // server no longer offering it — and only a candidates read says that
          // (C4), so the refusal is reconciled against one, taken HERE, because
          // an answer where every pick is withdrawn opens no picker and a read
          // that only the picker performs would never happen.
          //
          // Withdrawn takes the row and the agreement together: the row that
          // disappears and the count that falls are the whole report, and the
          // entry goes with it so nothing is left describing a row this dialog
          // no longer has. An offered id keeps its row and takes the read's own
          // candidate as its agreement — both halves from one read, including an
          // empty supplier list, which re-asks as a row with no chips and saves
          // as an empty promise the server seeds an empty route for. Only picks:
          // a supplier disagreement may not delete a row the server already
          // holds, and one this write promised nothing for is no part of it.
          const saved = new Set(baselineModels.map((model) => model.id));
          const disputed = Object.entries(failure.changedSuppliers)
            .filter(([modelId]) => chosenRef.current.has(modelId) && !saved.has(modelId));
          // A read that failed knows nothing about what is offered, so it
          // withdraws nothing and every disputed pick is asked again, promising
          // the suppliers the refusal itself named — the freshest word there is
          // when the read that would supersede it never arrived. The picker
          // reads on its way in, so a withdrawal invisible here is still caught
          // there; a row deleted on a failed read would be this dialog spending
          // the user's list on its own missing evidence.
          const offered = disputed.length > 0 ? await offeredNow() : null;
          const withdrawn = new Set(
            offered ? disputed.filter(([modelId]) => !offered.has(modelId)).map(([modelId]) => modelId) : [],
          );
          const reask: string[] = [];
          for (const [modelId, suppliers] of disputed) {
            if (withdrawn.has(modelId)) {
              chosenRef.current.delete(modelId);
              continue;
            }
            const fresh = offered?.get(modelId);
            const pick = chosenRef.current.get(modelId);
            if (fresh) chosenRef.current.set(modelId, chosenCandidate(fresh));
            else if (pick) chosenRef.current.set(modelId, { ...pick, expected_suppliers: [...suppliers] });
            reask.push(modelId);
          }
          if (withdrawn.size > 0) mutate(draftRef.current.filter((model) => !withdrawn.has(model.id)));
          if (reask.length > 0) {
            setPicking({ seed: new Set(reask) });
            return;
          }
          // Nothing left to ask, and the list already shows what changed: a drop
          // is its own answer, and a sentence about it would be this dialog
          // reporting its own edit back to the user. But the save they pressed is
          // still owed — nothing was committed — so it goes again on the reduced
          // list rather than costing them a second press for a withdrawal they
          // did not make. It terminates: every pass either drops at least one
          // pick from the map that `disputed` is drawn from, or asks instead.
          if (withdrawn.size > 0) {
            void save();
            return;
          }
          // Nothing to ask and nothing to drop: a refusal about ids this write
          // promised nothing for is not one this dialog can answer, so it keeps
          // the failure sentence below rather than resolving into silence.
        }
        // The route guard. Nothing was committed, so this is not a failed save
        // but an unanswered question: the server has now stated what this exact
        // write would take with it, and the only thing missing is the user's
        // agreement to it.
        //
        // The statement is kept whole and unedited, bound to the write it
        // answers, because that is what the retry will echo. What follows
        // decides only whether the user has to be asked.
        const refusal = failure?.code === MODEL_IN_ROUTE ? failure : null;
        const guardedPlans = refusal
          ? refusalPlans(refusal.wouldRemoveHops, refusal.wouldInterrupt, intent.removed, backend)
          : [];
        if (refusal && guardedPlans.length > 0) {
          // Does the server's plan say what the user already accepted? Compared
          // as contents, not as sequence: the previews were recorded row by row
          // as the trash was clicked, the server walks its own baseline, and two
          // orders of the same consequences are not a disagreement about them.
          const shown = [...previewRef.current]
            .filter(([modelId]) => intent.removed.has(modelId))
            .map(([, plan]) => plan);
          // A picture contributes no gaps, because it never claimed any: so a
          // server plan that names an interruption is by construction not
          // covered by what the user has already accepted, and the question is
          // re-asked with the server's own words.
          const agreed = samePlanContents(refusal.wouldRemoveHops, shown.flatMap((plan) => plan.hops))
            && samePlanContents(refusal.wouldInterrupt, shown.flatMap((plan) => plan.gaps ?? []));
          refusalRef.current = {
            hops: refusal.wouldRemoveHops,
            gaps: refusal.wouldInterrupt,
            baseline: baselineModels,
            models: requested,
            // An agreement that already covers this plan owes nothing: the user
            // has seen these consequences, and `agreed` is that statement.
            // Otherwise every removal being handed back owes an answer, and the
            // echo waits for the last of them.
            //
            // What the split could not attribute to any removed row is owed by
            // nobody, because there is no row for it to be asked in. It still
            // travels in the echo once every question that COULD be asked has
            // been answered — the alternative is a save the user can never make
            // — and that residue is a recorded decision, not an oversight.
            owed: agreed ? new Set() : new Set(guardedPlans.map((question) => question.modelId)),
          };
          // Already agreed, and this attempt did not carry the agreement: the
          // user answered this question before they pressed Save, and asking it
          // again would only be the dialog telling them what they just told it.
          // The same list goes again, this time echoing the server's own plan.
          // It terminates — that retry is forced, so it cannot take this branch
          // a second time.
          if (agreed && body.force !== true) {
            void save();
            return;
          }
          // Not what was shown — a route created since the baseline was read, or
          // an Agent stranded by a removal that looked free — so every held-back
          // removal comes back into the draft and is asked again, through the
          // same confirmation, now carrying the server's own plan. The baseline
          // is deliberately NOT re-read: the refusal above answers this
          // `baseline` + `models` pair, and reading a newer list would leave the
          // dialog holding a plan about a write it can no longer make.
          //
          // It comes back to its own place, not to the end. The requested order
          // is the list with the row already gone, so the baseline is what says
          // where it belongs — and a removal the user then cancels leaves the
          // draft equal to the baseline, rows and order, which is the only state
          // that can honestly report itself as unedited.
          const heldBack = new Set(guardedPlans.map((question) => question.modelId));
          const held: BackendCatalogIntent = {
            ...intent,
            removed: new Set([...intent.removed].filter((id) => !heldBack.has(id))),
            order: orderWithRestored(intent.order, baselineModels.map((model) => model.id), heldBack),
          };
          mutate(applyBackendCatalogIntent(baselineModels, held));
          // What the user accepted for these rows was the preview, and it is not
          // what the server says. Confirming a question below records the
          // server's own account in its place.
          for (const modelId of heldBack) previewRef.current.delete(modelId);
          guardedRef.current = [...guardedPlans];
          askNextGuarded();
          return;
        }
        // A route that named its failure has decided what it did, and for this
        // endpoint 「decided」 can still mean 「wrote」: the server commits the
        // catalog and only then asks the backend to load it, so `engine_down`
        // names rows that reached the disk and never reached the runtime. That
        // is a failed save with a persisted list behind it, and no re-read can
        // turn it into a success. Only an answer this client never got to read
        // leaves the outcome genuinely unknown — and only that may be settled by
        // reading the server's own catalog back.
        const decided = failure?.serverNamed === true;
        const unloaded = decided && catalogSaveLeftUnloaded(failure?.code, failure?.detail);
        const reason = decided
          ? catalogSaveFailureKey(failure?.detail)
          : 'settings.models.gateway.catalog.saveFailed';
        try {
          const observed = await readBackendCatalogBaseline(modelsApi, backend);
          const current = observed.models;
          const landed = current !== null && backendCatalogIntentApplied(current, intent);
          if (current && landed && !decided) {
            applyBaseline(observed, current);
            await Promise.resolve(onSaved(observed.agent)).catch(() => {});
            onClose();
            return;
          }
          applyBaseline(observed, current ? applyBackendCatalogIntent(current, intent) : []);
          await Promise.resolve(onObserved(observed.agent)).catch(() => {});
          // Stored and out of use at the same time, and only for the code that
          // can leave a list that way: a validation or conflict refusal keeps
          // its own sentence even when the re-read happens to agree with the
          // draft, because that write never landed at all.
          setSaveFailedKey(landed && unloaded ? 'settings.models.gateway.catalog.saveNotApplied' : reason);
          return;
        } catch {
          setReadState('error');
        }
        setSaveFailedKey(reason);
        return;
      }
      await Promise.resolve(onSaved(echoed)).catch(() => {});
      onClose();
    });
  };

  const rowActions = (model: BackendModel) => (
    <div className="flex shrink-0 items-center gap-1.5">
      <button
        type="button"
        className="model-hub-catalog-action"
        aria-label={t('settings.models.gateway.catalog.edit', { model: model.display_name ?? model.id }) as string}
        disabled={busy}
        onClick={() => setEditing({ model })}
      >
        <Pencil className="size-[15px]" aria-hidden="true" />
      </button>
      <button
        type="button"
        className="model-hub-catalog-action model-hub-catalog-action--danger"
        aria-label={t('settings.models.gateway.catalog.remove', { model: model.display_name ?? model.id }) as string}
        disabled={busy}
        onClick={() => removeModel(model)}
      >
        <Trash2 className="size-[15px]" aria-hidden="true" />
      </button>
    </div>
  );

  const rowBody = (model: BackendModel) => {
    const renderedLabel = displayLabel(model);
    return (
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="model-hub-catalog-name truncate">{renderedLabel}</span>
        {renderedLabel !== model.id && <span className="model-hub-catalog-id truncate font-mono">{model.id}</span>}
      </span>
    );
  };

  /**
   * The question a routed removal asks, inside the row it is about.
   *
   * A route is a consequence the user did not choose when they chose the model,
   * so it is shown before it is taken — through the same evidence body every
   * other guarded Model Hub mutation shows, because the question is the same
   * question and an answer to it means the same thing. It renders the plan the
   * question was raised from: the baseline's route on a first ask, the server's
   * own refusal on a re-ask. Answering it removes the row and its route
   * together, in one transaction, when the list saves (C3).
   *
   * The page's Sources travel with it so each hop names its supplier. This
   * dialog still holds no Source concept — it does not resolve one, order one or
   * write one — but 「no hidden mappings」 is a rule about what the user is shown
   * before they agree, and 「a hop at position 2 disappears」 without whose hop it
   * was is exactly the hidden half.
   */
  const removeConfirmation = (model: BackendModel) => {
    if (removing?.modelId !== model.id) return null;
    const { plan } = removing;
    const asked = removing;
    return (
      <div className="model-hub-catalog-confirm">
        <div className="model-hub-catalog-consequence" role="alert">
          <GuardImpact hops={plan.hops} gaps={plan.gaps} sourceNames={sourceNames} />
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            className="model-hub-catalog-confirm-action rounded-md text-[12.5px] font-semibold"
            disabled={busy}
            onClick={askNextGuarded}
          >
            {t('settings.models.gateway.catalog.cancel')}
          </Button>
          <Button
            type="button"
            variant="destructive"
            className="model-hub-catalog-confirm-action rounded-md text-[12.5px] font-bold"
            disabled={busy}
            onClick={() => { acceptRemoval(asked); askNextGuarded(); }}
          >
            {t('settings.models.gateway.catalog.removeConfirm')}
          </Button>
        </div>
      </div>
    );
  };

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(next) => { if (!next && !busy) onClose(); }}
      >
        <DialogContent
          mobileSheetHeight="tall"
          closeLabel={t('settings.models.gateway.catalog.cancel') as string}
          className="model-hub-catalog-dialog flex h-[min(642px,calc(100dvh-32px))] w-[min(680px,calc(100vw-32px))] max-w-[680px] flex-col gap-0 overflow-hidden rounded-[14px] border-border-strong bg-surface p-0 shadow-[var(--model-hub-dialog-shadow)] max-md:w-full max-md:max-w-none max-md:rounded-t-2xl max-md:p-0 max-md:pt-2"
          onEscapeKeyDown={(event) => { if (busy || grabbedId) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
        >
          <DialogHeader className="model-hub-catalog-head shrink-0 justify-center border-b border-border">
            <DialogTitle className="model-hub-catalog-title">
              {t('settings.models.gateway.catalog.title', { backend: t(`settings.models.backends.${backend}`) })}
            </DialogTitle>
            <DialogDescription className="sr-only">{t('settings.models.gateway.catalog.description')}</DialogDescription>
          </DialogHeader>

          <div className="model-hub-catalog-body flex min-h-0 flex-1 flex-col">
            <div className="flex min-w-0 shrink-0 flex-col gap-2 sm:flex-row sm:items-center">
              <div className="model-hub-catalog-control model-hub-catalog-search flex min-w-0 flex-1 items-center gap-2">
                <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
                <Input
                  variant="bare"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('settings.models.gateway.catalog.search') as string}
                  aria-label={t('settings.models.gateway.catalog.search') as string}
                  className="min-w-0 flex-1 text-[12.5px]"
                  disabled={!editable || busy}
                />
              </div>
              {/* One action for both paths. The picker offers what this backend
                  and the user's providers already have, and hands off to the
                  editor for anything they do not — so 「add a model」 is one
                  decision with a fallback, not two entry points the user has to
                  choose between up front. It reads Sources, so it is offered
                  only where the page's other Source-reading surfaces are. */}
              {canReadSources && (
                <Button
                  type="button"
                  variant="brand"
                  className="model-hub-catalog-control shrink-0 rounded-md px-4 text-[12.5px]"
                  disabled={!editable || busy}
                  onClick={() => setPicking({ seed: NO_SEED })}
                >
                  <Plus aria-hidden="true" />
                  {t('settings.models.gateway.catalog.add')}
                </Button>
              )}
            </div>

            {ready && (draft.length > 0 || legacyIds.length > 0) && (
              <div className="model-hub-catalog-columns flex shrink-0 items-center justify-between">
                <span>{t('settings.models.gateway.catalog.columnModel')}</span>
                <span>{legacy ? t('settings.models.gateway.catalog.columnState') : t('settings.models.gateway.catalog.columnActions')}</span>
              </div>
            )}

            <div className="min-h-0 flex-1 overflow-y-auto">
              {!ready ? (
                <div className="flex flex-col items-center gap-3 px-4 py-12 text-center text-[12.5px] text-muted">
                  {readState === 'loading' ? (
                    <LoaderCircle className="size-5 animate-spin" aria-hidden="true" />
                  ) : (
                    <>
                      <p>{t('settings.models.gateway.catalog.baselineUnavailable')}</p>
                      <Button type="button" variant="outline" size="sm" onClick={() => loadBaseline(true)}>
                        {t('settings.models.gateway.retry')}
                      </Button>
                    </>
                  )}
                </div>
              ) : legacy ? (
                <div className="flex flex-col gap-2">
                  <p className="model-hub-catalog-legacy">{t('settings.models.gateway.catalog.legacy')}</p>
                  {legacyIds.map((modelId) => (
                    <div key={modelId} className="model-hub-catalog-row model-hub-catalog-row--static flex min-w-0 items-center gap-3">
                      <span className="model-hub-catalog-name min-w-0 flex-1 truncate font-mono">{modelId}</span>
                    </div>
                  ))}
                </div>
              ) : visible.length === 0 ? (
                <p className="px-1 py-10 text-center text-[12.5px] text-muted">
                  {t(filtering ? 'settings.models.gateway.catalog.noMatch' : 'settings.models.gateway.catalog.empty')}
                </p>
              ) : (
                <Reorder.Group
                  axis="y"
                  as="ul"
                  values={movableIds}
                  onReorder={reorderMovable}
                  className="flex list-none flex-col gap-[var(--model-hub-catalog-row-gap)] p-0"
                >
                  {visible.map((model) => (
                    model.locked
                      ? (
                        <li key={model.id} className="model-hub-catalog-row model-hub-catalog-row--locked flex min-w-0 flex-col justify-center">
                          <div className="flex min-w-0 items-center gap-3">
                            <Lock className="model-hub-catalog-grip-icon shrink-0" aria-hidden="true" />
                            {rowBody(model)}
                            <span className="model-hub-pill model-hub-fill-0a shrink-0 border border-border text-muted">
                              {t('settings.models.gateway.catalog.lockedBadge')}
                            </span>
                          </div>
                        </li>
                      )
                      : (
                        <CatalogRow
                          key={model.id}
                          model={model}
                          grabbed={grabbedId === model.id}
                          confirming={removing?.modelId === model.id}
                          draggable={!filtering && !busy}
                          registerGrip={(node) => {
                            if (node) grips.current.set(model.id, node);
                            else grips.current.delete(model.id);
                          }}
                          onGripKeyDown={(event) => handleGripKey(model.id, event)}
                          body={rowBody(model)}
                          actions={rowActions(model)}
                          note={removeConfirmation(model)}
                        />
                      )
                  ))}
                </Reorder.Group>
              )}
            </div>
            <p aria-live="polite" className="sr-only">
              {announcement ? t(`settings.models.gateway.catalog.${announcement.key}`, announcement) : ''}
            </p>
          </div>

          <DialogFooter className="model-hub-catalog-foot shrink-0 items-center border-t border-border sm:justify-between">
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="model-hub-catalog-count">
                {t('settings.models.gateway.modelCount', { count: legacy ? legacyIds.length : draft.length })}
              </span>
              {saveFailedKey && (
                <span className="model-hub-catalog-blocked" role="status">{t(saveFailedKey)}</span>
              )}
            </div>
            <div className="flex w-full flex-col-reverse gap-2 sm:w-auto sm:flex-row">
              <Button
                type="button"
                variant="outline"
                className="model-hub-catalog-control rounded-md px-5 text-[12.5px] font-semibold"
                onClick={onClose}
                disabled={busy}
              >
                {t('settings.models.gateway.catalog.cancel')}
              </Button>
              <Button
                type="button"
                variant="brand"
                className="model-hub-catalog-control rounded-md px-5 text-[12.5px] font-semibold"
                onClick={save}
                disabled={!editable || (!dirty && !retryable) || busy}
              >
                {busy && <LoaderCircle className="animate-spin" aria-hidden="true" />}
                {t(busy ? 'settings.models.gateway.catalog.saving' : 'settings.models.gateway.catalog.save')}
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {picking && (
        <BackendModelPickerDialog
          open
          backend={backend}
          listedIds={offerable(takenIds, picking.seed)}
          seedPicked={picking.seed}
          // 「Add none of these」 — the same answer as confirming with nothing
          // picked, and the same call, because a re-ask the user dismisses has
          // declined every id it seeded. An ordinary add seeds nothing, so this
          // still just closes.
          onCancel={() => addCandidates([])}
          onAdd={addCandidates}
          // The editor is the other door an id becomes a row through, so leaving
          // by it answers this picker's two standing questions in the same order
          // every other exit answers them, through the same call.
          //
          // `addCandidates([])` first, for the same reason Cancel makes that
          // call: walking out through the editor confirms none of a re-ask's
          // seeded ids, and a seeded projection this dialog keeps is one the
          // next Save would send as if it had been agreed.
          //
          // Then the id, resolved BEFORE the row lookup and through the rule the
          // editor itself commits under (`backendModelId`, which `draftWithId`
          // applies there). The lookup is a total function of the id, so the id
          // it is asked about has to be the one the row would be saved as: asked
          // about a typed `foo` it does not find the user's saved `custom/foo`,
          // and the editor — resolving the id only on commit — would then write
          // a blank row onto it, dropping the limits, modalities, capabilities
          // and name they had described. The resolver is idempotent, so the
          // editor re-applying it to this seed changes nothing.
          onCustom={(typed) => {
            addCandidates([]);
            const seedId = backendModelId(typed, backend, standardVendors);
            const existing = heldRowFor(seedId, draftRef.current, baselineRef.current?.models ?? []);
            setEditing(existing ? { model: existing } : { model: null, seedId });
          }}
        />
      )}

      {editing && (
        <BackendModelEditorDialog
          open
          backend={backend}
          model={editing.model}
          seedId={editing.seedId}
          takenIds={takenIds}
          effortSuggestions={effortSuggestions}
          standardVendors={standardVendors}
          onCancel={() => setEditing(null)}
          onCommit={commitEdit}
        />
      )}
    </>
  );
};

const CatalogRow: React.FC<{
  model: BackendModel;
  grabbed: boolean;
  confirming: boolean;
  draggable: boolean;
  registerGrip: (node: HTMLButtonElement | null) => void;
  onGripKeyDown: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
  body: React.ReactNode;
  actions: React.ReactNode;
  note: React.ReactNode;
}> = ({ model, grabbed, confirming, draggable, registerGrip, onGripKeyDown, body, actions, note }) => {
  const { t } = useTranslation();
  const controls = useDragControls();
  return (
    <Reorder.Item
      value={model.id}
      dragListener={false}
      dragControls={controls}
      className={cn(
        'model-hub-catalog-row flex min-w-0 list-none flex-col justify-center',
        confirming && 'is-confirming',
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        <button
          ref={registerGrip}
          type="button"
          aria-label={t('settings.models.gateway.catalog.reorder', { model: model.display_name ?? model.id }) as string}
          aria-grabbed={grabbed}
          disabled={!draggable}
          onPointerDown={(event) => { if (draggable) controls.start(event); }}
          onKeyDown={onGripKeyDown}
          className={cn(
            'model-hub-catalog-grip cursor-grab active:cursor-grabbing disabled:cursor-not-allowed',
            grabbed && 'is-grabbed',
          )}
        >
          <GripVertical className="model-hub-catalog-grip-icon" aria-hidden="true" />
        </button>
        {body}
        {actions}
      </div>
      {note}
    </Reorder.Item>
  );
};

export default BackendModelCatalogDialog;
