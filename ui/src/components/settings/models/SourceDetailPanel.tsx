import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Info, Loader2, LogIn, MoreHorizontal, Pencil, Plus, RefreshCw, Search, Trash2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Input } from '@/components/ui/input';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { cn } from '@/lib/utils';
import { formatRelativeTime } from '@/lib/relativeTime';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { PROTOCOL_COPY_KEYS } from './addApiKeyState';
import { classifyModelHubFailure } from './asyncLifetime';
import { Field } from './dialogFields';
import { GuardImpact } from './GuardImpact';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import {
  assessSourceEdit,
  canEditSourceEndpoint,
  manageActions,
  MANAGE_DESTINATION,
  MANAGE_LABEL_KEY,
  SOURCE_EDIT_REASON_KEY,
  transitionManageStage,
} from './manage';
import type {
  ManageGuardPlan,
  ManageCommitAction,
  ManageKind,
  ManageStage,
  SourceEditDraft,
} from './manage';
import { apiFailure, modelsApi, type GuardConfirmation } from './modelsApi';
import {
  SOURCE_PROVIDER_COPY_KEYS,
  sourceProviderIdentity,
} from './sourcePresentation';
import {
  sourceMutationReadScope,
  type PresentSourceMutationCommit,
  type SourceMutationLanding,
  type SourceMutationReadScope,
  type SourceMutationSettlement,
  type TrackSourceMutation,
} from './mutationSettlement';
import { handOffProviderTab } from './providerTab';
import { reconcileUnknownWrite } from './reconcileUnknownWrite';
import { REPAIR_DESTINATION, REPAIR_LABEL_KEY, reauthBodyKey, reauthCost, repairAction } from './repair';
import { tierEditRefusedAsManaged } from './serverCopy';
import { activeSourceAdoption, sourceStatePresentation } from './sourceStatePresentation';
import {
  managedTierSource,
  tierMutationPayload,
  type ManagedTierSource,
  type TierMutationIntent,
} from './tierMutation';
import { TIER_SUGGESTIONS } from './tierSuggestions';
import { useDeadlineClock } from './useDeadlineClock';
import { ACCENT_ICON, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type {
  AgentBackend,
  RouteHopRef,
  Source,
  SourcePatch,
  SourceProtocol,
  SuppliedModel,
  SupplyGap,
} from './types';

const ManualModelMenu: React.FC<{
  model: SuppliedModel;
  busy: boolean;
  onRemove: () => void;
}> = ({ model, busy, onRemove }) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const label = `${t('settings.models.sourceDetail.row.remove')} ${model.id}`;
  return (
    <ResponsiveMenu
      open={open}
      onOpenChange={setOpen}
      sheetTitle={model.id}
      className="w-40"
      trigger={<button type="button" disabled={busy} aria-label={label} title={label} className="model-hub-source-row-action grid place-items-center text-destructive-ink hover:bg-destructive/[0.08]"><Trash2 className="size-3.5" /></button>}
    >
      <button type="button" role="menuitem" className="flex w-full items-center rounded-md px-2.5 py-2 text-left text-[12px] font-semibold text-destructive-ink hover:bg-destructive/[0.08]" onClick={() => { setOpen(false); onRemove(); }}>{t('settings.models.sourceDetail.row.remove')}</button>
    </ResponsiveMenu>
  );
};

const SourceManageMenu: React.FC<{
  source: Source;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
}> = ({ source, busy, onEdit, onDelete }) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const label = t('settings.models.sourceDetail.manage.label', { source: source.display_name }) as string;
  const activate = (kind: ManageKind) => {
    setOpen(false);
    if (kind === 'edit_source') onEdit();
    else onDelete();
  };
  return (
    <ResponsiveMenu
      open={open}
      onOpenChange={setOpen}
      sheetTitle={source.display_name}
      className="w-44"
      trigger={<button type="button" disabled={busy} aria-label={label} title={label} className="model-hub-source-more grid place-items-center text-muted hover:bg-surface-2 hover:text-foreground"><MoreHorizontal className="size-4" /></button>}
    >
      {manageActions().map((kind) => (
        <button
          key={kind}
          type="button"
          role="menuitem"
          data-manage-kind={kind}
          data-manage-destination={MANAGE_DESTINATION[kind]}
          className={cn(
            'flex w-full items-center rounded-md px-2.5 py-2 text-left text-[12px] font-semibold hover:bg-surface-2',
            kind === 'delete_source' && 'text-destructive-ink hover:bg-destructive/[0.08]',
          )}
          onClick={() => activate(kind)}
        >
          {t(MANAGE_LABEL_KEY[kind])}
        </button>
      ))}
    </ResponsiveMenu>
  );
};

type GuardedAction =
  | { kind: 'refetch'; plan: ManageGuardPlan }
  | { kind: 'removeModel'; model: SuppliedModel; plan: ManageGuardPlan };

const confirmGuardPlan = (plan: ManageGuardPlan): GuardConfirmation => ({
  force: true,
  would_remove_hops: plan.hops,
  would_interrupt: plan.gaps,
});

const GUARD_COPY_KIND: Record<GuardedAction['kind'], 'refetch' | 'removeModel'> = {
  refetch: 'refetch',
  removeModel: 'removeModel',
};

const committedPlan = (hops: RouteHopRef[], gaps: SupplyGap[]): ManageGuardPlan | null =>
  hops.length > 0 || gaps.length > 0 ? { hops, gaps } : null;

type SourceReconciliation =
  | { kind: 'source'; source: Source }
  | { kind: 'gone'; sources: Source[]; snapshot: number };

/** The badge that says which rung declared a locked model's tiers. */
const TIER_PROVENANCE_LABEL_KEY: Readonly<Record<ManagedTierSource, string>> = {
  upstream: 'settings.models.sourceDetail.tiers.managed.upstream',
  catalog: 'settings.models.sourceDetail.tiers.managed.catalog',
};

/**
 * A tier write that did not land, and whether anything is left to try.
 *
 * `retryable` carries the intent because "Try again" has to replay the exact
 * write the rollback undid. `managed` carries none on purpose: the server owns
 * that model's declaration, so there is no version of this write that succeeds,
 * and offering a button that re-asks a settled question would be the one wrong
 * affordance to put in front of this user.
 */
type TierFailure =
  | { kind: 'retryable'; intent: TierMutationIntent }
  | { kind: 'managed' };

const TierEditor: React.FC<{
  model: SuppliedModel;
  protocol: SourceProtocol;
  editing: boolean;
  onEdit: () => void;
  onClose: () => void;
  onMutating: () => void;
  trackMutation: TrackSourceMutation;
}> = ({ model, protocol, editing, onEdit, onClose, onMutating, trackMutation }) => {
  const { t } = useTranslation();
  const managed = managedTierSource(model.reasoning_efforts_source);
  const [tiers, setTiers] = React.useState(model.reasoning_efforts ?? []);
  const [draft, setDraft] = React.useState('');
  const [saving, setSaving] = React.useState(false);
  const [failed, setFailed] = React.useState<TierFailure | null>(null);
  const [returnFocus, setReturnFocus] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const cellRef = React.useRef<HTMLButtonElement>(null);
  React.useEffect(() => setTiers(model.reasoning_efforts ?? []), [model.reasoning_efforts]);
  // Which row is open belongs to the table, not to the row — that is what makes
  // "one editor at a time" structural rather than a convention every collapse
  // path has to remember. What the row drops on the way out is the uncommitted
  // draft, and only that: a rolled-back write is not a draft, so it stays with
  // the row that produced it (below) rather than with whoever holds the editor.
  React.useEffect(() => { if (!editing) setDraft(''); }, [editing]);
  // Escape is the one collapse the keyboard asks for, so it is the one that owes
  // a place to land; a click elsewhere already chose one.
  React.useEffect(() => {
    if (editing || !returnFocus) return;
    cellRef.current?.focus();
    setReturnFocus(false);
  }, [editing, returnFocus]);

  const commit = async (intent: TierMutationIntent): Promise<boolean> => {
    if (saving) return false;
    setSaving(true);
    // Every in-editor control is transient — a suggestion becomes a chip, a chip
    // disappears — so acting on one has to hand focus back to the field that
    // outlives them all, before the element holding it unmounts onto the body.
    (inputRef.current ?? cellRef.current)?.focus();
    try {
      return await trackMutation(async (latest, settlement) => {
        const payload = tierMutationPayload(latest, model.id, intent);
        if (!payload) {
          settlement.release();
          return false;
        }
        const { previous, next } = payload;
        onMutating();
        setFailed(null);
        setTiers(next);
        if (previous.length === next.length && previous.every((tier, index) => tier === next[index])) {
          settlement.release();
          return true;
        }
        try {
          const echoed = await modelsApi.updateModelReasoningEfforts(latest.id, model.id, next);
          await settlement.source(echoed);
          return true;
        } catch (error) {
          setTiers(previous);
          const refusal = apiFailure(error);
          setFailed(tierEditRefusedAsManaged(refusal) ? { kind: 'managed' } : { kind: 'retryable', intent });
          if (refusal?.code === 'source_not_found') await settlement.gone(latest.id);
          // A managed refusal re-reads for the same reason every other failed
          // write does, and one more: this client believed the row was editable
          // and the server says otherwise, so the read is what replaces the
          // stale editor with the provenance the server actually holds.
          else await settlement.unread();
          return false;
        }
      });
    } finally {
      setSaving(false);
    }
  };
  const add = async () => {
    const value = draft.trim();
    if (!value || tiers.includes(value)) return;
    if (await commit({ kind: 'add', tier: value })) setDraft('');
  };
  const retry = async () => {
    if (failed?.kind !== 'retryable') return;
    const { intent } = failed;
    if (await commit(intent) && intent.kind === 'add' && draft.trim() === intent.tier) setDraft('');
  };
  // A rolled-back write is the row's own unfinished business: the tier list is
  // already back to what the server holds, and "Try again" is the only way forward
  // from there. So the notice renders in whichever state the row is in and leaves
  // only through a write that lands — never because the editor closed or moved on.
  //
  // A refusal is the exception, and the one that outlives the editor: the re-read
  // it triggers turns the row into the locked one below, so its notice has to be
  // rendered by that branch too, or the only explanation the user gets would
  // disappear at the moment the row changes under them.
  //
  // Provenance can also reach that branch after an ordinary failure — the re-read
  // the rollback triggers, or a refresh from anywhere else, comes back with the
  // server owning the list — and it answers the retry the same way the refusal
  // did: `tierMutationPayload` declines a managed model, so the button would
  // replay a write that can no longer land. Which notice to show is therefore read
  // from the row's current provenance rather than frozen when the write failed;
  // that also lets the retry come back untouched if the server hands the list back.
  const failure = failed && (managed || failed.kind === 'managed' ? (
    <span data-tier-failure="managed" className="model-hub-source-tier inline-flex items-center gap-1.5 text-destructive-ink">
      {t('settings.models.sourceDetail.fail.tierManaged')}
    </span>
  ) : (
    <span data-tier-failure="retryable" className="model-hub-source-tier inline-flex items-center gap-1.5 text-destructive-ink">
      {t('settings.models.sourceDetail.fail.tier')}
      <button type="button" disabled={saving} onClick={() => void retry()} className="font-semibold underline underline-offset-2 disabled:opacity-50">{t('settings.models.sourceDetail.retry')}</button>
    </span>
  ));
  // Rung 1 and 2 of the provenance ladder are the server's declaration, re-applied
  // on every refresh: there is nothing for the user to add and nothing to delete,
  // so the cell stops being a way in rather than becoming a disabled one. It says
  // which rung instead — a row that simply refused to open would leave "why" as
  // the user's problem.
  if (managed) {
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        <div data-tier-provenance={managed} className="model-hub-source-tier-cell flex min-w-0 flex-wrap items-center gap-1.5">
          {tiers.length > 0 ? tiers.map((tier) => (
            <span key={tier} className="model-hub-source-tier model-hub-source-tier-chip inline-flex rounded-full border border-border font-mono text-foreground">{tier}</span>
          )) : <span className="model-hub-source-tier-empty">{t('settings.models.sourceDetail.tiers.empty')}</span>}
          <span
            title={t('settings.models.sourceDetail.tiers.managedHint') as string}
            className="model-hub-source-pill model-hub-source-entry-pill w-fit rounded-full border font-semibold"
          >
            {t(TIER_PROVENANCE_LABEL_KEY[managed])}
          </span>
        </div>
        {failure}
      </div>
    );
  }
  if (!editing) {
    // The whole cell is the edit entry, which is what lets the add affordance be
    // drawn only under a pointer: 20 rows each carrying a permanent 「+ 添加档位」
    // pill turn an inventory table into a wall of buttons. It stays in the box it
    // reserves rather than being removed from it, so revealing it moves nothing.
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        <button
          ref={cellRef}
          type="button"
          className="model-hub-source-tier-cell flex min-w-0 flex-wrap items-center gap-1.5 text-left"
          onClick={onEdit}
        >
          {tiers.length > 0 ? tiers.map((tier) => (
            <span key={tier} className="model-hub-source-tier model-hub-source-tier-chip inline-flex rounded-full border border-border font-mono text-foreground">{tier}</span>
          )) : <span className="model-hub-source-tier-empty">{t('settings.models.sourceDetail.tiers.empty')}</span>}
          <span className="model-hub-source-tier model-hub-source-tier-add model-hub-source-tier-reveal inline-flex rounded-full border font-semibold">{t(tiers.length > 0 ? 'settings.models.sourceDetail.tiers.add' : 'settings.models.sourceDetail.tiers.addFirst')}</span>
        </button>
        {failure}
      </div>
    );
  }
  const suggestions = TIER_SUGGESTIONS[protocol].filter((tier) => !tiers.includes(tier));
  // The editor collapses when focus leaves the editor, not when the input alone
  // does. Keyed to the input, every control inside had to defend itself against
  // its own focus — which a pointer can fake by refusing it and a keyboard
  // cannot, so Tab closed the row before it could reach a suggestion at all.
  // Containment is that same rule stated once, at the boundary it is about.
  return (
    <div
      data-source-dialog-local-escape
      className="flex min-w-0 flex-col gap-1.5"
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) onClose(); }}
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return;
        event.preventDefault();
        setDraft('');
        setReturnFocus(true);
        onClose();
      }}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        {tiers.map((tier) => (
          <span key={tier} className="model-hub-source-tier model-hub-source-tier-chip inline-flex items-center gap-1 rounded-full border border-border font-mono text-foreground">
            {tier}
            <button type="button" disabled={saving} onClick={() => void commit({ kind: 'remove', tier })} aria-label={t('settings.models.sourceDetail.tiers.remove', { tier }) as string} className="text-muted hover:text-foreground disabled:opacity-50">
              <X className="size-2.5" />
            </button>
          </span>
        ))}
        <Input
          ref={inputRef}
          autoFocus
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void add(); } }}
          disabled={saving}
          placeholder={t('settings.models.sourceDetail.tiers.inputHint') as string}
          className="model-hub-source-tier h-7 w-28 rounded-full border-mint/40 px-2.5"
        />
        {/* A suggestion adds through the same path typing it would take. */}
        {suggestions.map((tier) => (
          <button
            key={tier}
            type="button"
            disabled={saving}
            onClick={() => void commit({ kind: 'add', tier })}
            aria-label={t('settings.models.sourceDetail.tiers.suggest', { tier }) as string}
            className="model-hub-source-tier model-hub-source-tier-suggest inline-flex rounded-full border font-mono disabled:opacity-50"
          >
            {tier}
          </button>
        ))}
        {failure}
      </div>
      {/* The two questions a free-text field cannot answer by itself: whether
          anything validates what is typed, and what an empty row costs. */}
      <p className="model-hub-source-tier-note">{t('settings.models.sourceDetail.tiers.note')}</p>
    </div>
  );
};

const DraftTiers: React.FC<{
  tiers: string[];
  onChange: (tiers: string[]) => void;
}> = ({ tiers, onChange }) => {
  const { t } = useTranslation();
  const [draft, setDraft] = React.useState('');
  const add = () => {
    const value = draft.trim();
    if (!value || tiers.includes(value)) return;
    onChange([...tiers, value]);
    setDraft('');
  };
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {tiers.map((tier) => (
        <span key={tier} className="model-hub-source-tier model-hub-source-tier-chip inline-flex items-center gap-1 rounded-full border border-border font-mono text-foreground">
          {tier}
          <button type="button" onClick={() => onChange(tiers.filter((item) => item !== tier))} aria-label={t('settings.models.sourceDetail.tiers.remove', { tier }) as string} className="text-muted hover:text-foreground"><X className="size-2.5" /></button>
        </span>
      ))}
      <Input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') { event.preventDefault(); add(); }
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setDraft(''); event.currentTarget.blur(); }
        }}
        placeholder={t('settings.models.sourceDetail.tiers.inputHint') as string}
        className="model-hub-source-tier h-7 w-36 rounded-full border-mint/40 px-2.5"
      />
    </div>
  );
};

export const SourceDetailPanel: React.FC<{
  source: Source;
  trackMutation: TrackSourceMutation;
  /**
   * Start the re-login journey for this source. REQUIRED, and required for the
   * reason §4.5 exists: `repair.ts` decided the one-tap remedy and
   * `OAuthConnectDialog` has run the re-auth journey since it shipped, but no
   * mount ever connected them, so a stopped subscription rendered its cause and
   * stopped — the dead end that sentence forbids. Optional here, the same mount
   * could forget again silently.
   *
   * The journey itself belongs to the caller rather than to this panel: a
   * re-login can strand OTHER agents' models (`interrupted_pairs`), so what it
   * invalidates is wider than the one row this panel owns.
   */
  onReauth: (source: Source) => void;
  /** Owns every post-commit report and read verdict outside this entity panel. */
  onMutationCommitted: PresentSourceMutationCommit;
  /** Stable focus target for callers that navigate into this detail surface. */
  headingRef?: React.Ref<HTMLHeadingElement>;
  /** Authoritative backends still using the gateway, when the Agent read is ready. */
  activeBackends?: ReadonlySet<AgentBackend>;
}> = ({ source, trackMutation, onReauth, onMutationCommitted, headingRef, activeBackends }) => {
  const { t, i18n } = useTranslation();
  const now = useDeadlineClock(source.state.status === 'cooldown' ? source.state.retry_at : null);
  const { Icon, accent } = sourceVisual(source);
  const [busy, setBusy] = React.useState(false);
  const [confirmingReauth, setConfirmingReauth] = React.useState(false);
  const [replacingKey, setReplacingKey] = React.useState(false);
  const [manageStage, dispatchManageStage] = React.useReducer(transitionManageStage, { kind: 'idle' });
  const [manualDraft, setManualDraft] = React.useState<{ modelId: string; tiers: string[]; failed: boolean; retryRead: boolean } | null>(null);
  const [editingTiers, setEditingTiers] = React.useState<string | null>(null);
  const [guard, setGuard] = React.useState<GuardedAction | null>(null);
  const [result, setResult] = React.useState<{ added: string[]; removed: string[] } | null>(null);
  const [refetchFailed, setRefetchFailed] = React.useState(false);
  const [removeFailure, setRemoveFailure] = React.useState<{ modelId: string; retryRead: boolean } | null>(null);
  const [query, setQuery] = React.useState('');
  const models = source.models;
  const normalizedQuery = query.trim().toLocaleLowerCase(i18n.language);
  const filteredModels = normalizedQuery
    ? models.filter((model) => model.id.toLocaleLowerCase(i18n.language).includes(normalizedQuery))
    : models;
  const visibleModelIds = new Set(filteredModels.map((model) => model.id));
  const editDraft = 'draft' in manageStage ? manageStage.draft : null;
  const editAssessment = editDraft
    ? assessSourceEdit(source, editDraft)
    : { valid: false as const, patch: null, reason: null };

  const beginEdit = () => {
    dispatchManageStage({
      type: 'begin_edit',
      draft: { displayName: source.display_name, baseUrl: source.base_url ?? '' },
    });
  };
  const beginDelete = () => {
    dispatchManageStage({ type: 'begin_delete' });
  };

  const guardedFailure = (error: unknown): ManageGuardPlan | null => {
    const failure = apiFailure(error);
    if (!failure || (failure.wouldRemoveHops.length === 0 && failure.wouldInterrupt.length === 0)) return null;
    return { hops: failure.wouldRemoveHops, gaps: failure.wouldInterrupt };
  };
  const refetch = (confirmation?: GuardConfirmation) => {
    if (busy) return Promise.resolve();
    setResult(null);
    setRefetchFailed(false);
    setBusy(true);
    return trackMutation(async (latest, settlement) => {
      const before = new Set(latest.models.map((model) => model.id));
      try {
        const answer = await modelsApi.refreshSource(latest.id, confirmation);
        const after = new Set(answer.source.models.map((model) => model.id));
        const added = [...after].filter((id) => !before.has(id));
        const removed = [...before].filter((id) => !after.has(id));
        setResult({ added, removed });
        setGuard(null);
        await settlement.source(answer.source);
      } catch (error) {
        if (apiFailure(error)?.code === 'source_not_found') await settlement.gone(latest.id);
        else {
          const refusal = guardedFailure(error);
          if (refusal) {
            setGuard({ kind: 'refetch', plan: refusal });
            settlement.release();
          } else {
            setRefetchFailed(true);
            try {
              const inventory = await settlement.readInventory();
              const current = inventory.sources.find((item) => item.id === latest.id);
              if (current) await settlement.source(current);
              else await settlement.gone(latest.id, inventory);
            } catch {
              settlement.release();
            }
          }
        }
      }
    }).finally(() => setBusy(false));
  };
  const reconcileManualCreate = async (sourceId: string, modelId: string, settlement: SourceMutationSettlement) => reconcileUnknownWrite(
    settlement.readInventory,
    ({ sources, snapshot }) => {
      const current = sources.find((item) => item.id === sourceId);
      if (!current) return { kind: 'gone', sources, snapshot } satisfies SourceReconciliation;
      return current.models.some((model) => model.id === modelId)
        ? { kind: 'source', source: current } satisfies SourceReconciliation
        : undefined;
    },
  );
  const applyReconciliation = async (
    sourceId: string,
    value: SourceReconciliation,
    settlement: SourceMutationSettlement,
    scope?: SourceMutationReadScope,
  ) => {
    if (value.kind === 'gone') return settlement.gone(sourceId, value, scope);
    return settlement.source(value.source, scope);
  };
  const addManualModel = () => {
    if (!manualDraft || busy) return;
    const draft = manualDraft;
    const modelId = draft.modelId.trim();
    if (!modelId) return;
    setResult(null);
    setRefetchFailed(false);
    setBusy(true);
    return trackMutation(async (latest, settlement) => {
      if (latest.models.some((model) => model.id === modelId)) {
        setManualDraft(null);
        settlement.release();
        return;
      }
      if (draft.retryRead) {
        const reconciliation = await reconcileManualCreate(latest.id, modelId, settlement);
        if (reconciliation.kind === 'committed') {
          setManualDraft(null);
          await applyReconciliation(latest.id, reconciliation.value, settlement);
        } else {
          settlement.release();
          setManualDraft((current) => current ? {
            ...current,
            failed: true,
            retryRead: reconciliation.kind === 'unread',
          } : current);
        }
        return;
      }
      try {
        const echoed = await modelsApi.addCustomModel(latest.id, {
          model_id: modelId,
          display_name: null,
          reasoning_efforts: draft.tiers,
        });
        setManualDraft(null);
        setResult(null);
        await settlement.source(echoed);
      } catch (error) {
        if (apiFailure(error)?.code === 'source_not_found') {
          await settlement.gone(latest.id);
        } else {
          const reconciliation = await reconcileManualCreate(latest.id, modelId, settlement);
          if (reconciliation.kind === 'committed') {
            setManualDraft(null);
            await applyReconciliation(latest.id, reconciliation.value, settlement);
          } else {
            settlement.release();
            setManualDraft((current) => current ? {
              ...current,
              failed: true,
              retryRead: reconciliation.kind === 'unread',
            } : current);
          }
        }
      }
    }).finally(() => setBusy(false));
  };
  const reconcileRemoval = async (sourceId: string, model: SuppliedModel, settlement: SourceMutationSettlement) => {
    const reconciliation = await reconcileUnknownWrite(
      settlement.readInventory,
      ({ sources, snapshot }) => {
        const current = sources.find((item) => item.id === sourceId);
        if (!current) return { kind: 'gone', sources, snapshot } satisfies SourceReconciliation;
        return !current.models.some((entry) => entry.id === model.id)
          ? { kind: 'source', source: current } satisfies SourceReconciliation
          : undefined;
      },
    );
    if (reconciliation.kind === 'committed') {
      setGuard(null);
      setRemoveFailure(null);
      await applyReconciliation(sourceId, reconciliation.value, settlement);
      return;
    }
    settlement.release();
    setRemoveFailure({ modelId: model.id, retryRead: reconciliation.kind === 'unread' });
  };
  const remove = (model: SuppliedModel, confirmation?: GuardConfirmation) => {
    if (busy) return Promise.resolve();
    setResult(null);
    setRefetchFailed(false);
    setRemoveFailure(null);
    setBusy(true);
    return trackMutation(async (latest, settlement) => {
      if (!latest.models.some((candidate) => candidate.id === model.id)) {
        setGuard(null);
        settlement.release();
        return;
      }
      try {
        const echoed = await modelsApi.deleteCustomModel(latest.id, model.id, confirmation);
        setGuard(null);
        await settlement.source(echoed);
      } catch (error) {
        if (apiFailure(error)?.code === 'source_not_found') await settlement.gone(latest.id);
        else {
          const refusal = guardedFailure(error);
          if (refusal) {
            setGuard({ kind: 'removeModel', model, plan: refusal });
            settlement.release();
          } else await reconcileRemoval(latest.id, model, settlement);
        }
      }
    }).finally(() => setBusy(false));
  };
  const commitManagementMutation = async (
    action: ManageCommitAction,
    impact: ManageGuardPlan | null,
    settle: (scope: SourceMutationReadScope) => Promise<SourceMutationLanding>,
  ) => {
    dispatchManageStage({ type: 'commit', action });
    const scope = sourceMutationReadScope(impact);
    await onMutationCommitted({ action, impact, settle: () => settle(scope) });
    dispatchManageStage({ type: 'settled' });
  };
  const reconcileEditWrite = async (
    before: Source,
    draft: SourceEditDraft,
    patch: SourcePatch,
    plan: ManageGuardPlan | null,
    settlement: SourceMutationSettlement,
  ) => {
    const reconciliation = await reconcileUnknownWrite(
      settlement.readInventory,
      ({ sources, snapshot }) => {
        const current = sources.find((item) => item.id === before.id);
        if (!current) return { kind: 'gone', sources, snapshot } satisfies SourceReconciliation;
        const displayNameCommitted = patch.display_name === undefined
          || current.display_name === patch.display_name;
        const baseUrlCommitted = patch.base_url === undefined
          || current.base_url === patch.base_url;
        return displayNameCommitted && baseUrlCommitted
          ? { kind: 'source', source: current } satisfies SourceReconciliation
          : undefined;
      },
    );
    if (reconciliation.kind === 'committed') {
      const current = reconciliation.value.kind === 'source'
        ? reconciliation.value.source
        : null;
      await commitManagementMutation(
        'edit',
        current ? plan : null,
        (scope) => applyReconciliation(before.id, reconciliation.value, settlement, scope),
      );
      return;
    }
    settlement.release();
    dispatchManageStage({
      type: 'fail_edit',
      draft,
      patch,
      plan,
      forced: plan !== null,
      retryRead: reconciliation.kind === 'unread',
      before,
    });
  };
  const reconcileDeleteWrite = async (
    before: Source,
    plan: ManageGuardPlan | null,
    settlement: SourceMutationSettlement,
  ) => {
    const reconciliation = await reconcileUnknownWrite(
      settlement.readInventory,
      ({ sources, snapshot }) => sources.some((item) => item.id === before.id)
        ? undefined
        : { sources, snapshot },
    );
    if (reconciliation.kind === 'committed') {
      await commitManagementMutation(
        'delete',
        plan,
        (scope) => settlement.gone(before.id, reconciliation.value, scope),
      );
      return;
    }
    settlement.release();
    dispatchManageStage({
      type: 'fail_delete',
      plan,
      forced: plan !== null,
      retryRead: reconciliation.kind === 'unread',
      before,
    });
  };
  const submitEdit = (draft: SourceEditDraft, patch: SourcePatch, plan: ManageGuardPlan | null) => {
    if (busy) return Promise.resolve();
    const forced = plan !== null;
    dispatchManageStage({
      type: 'submit_edit',
      draft,
      patch,
      plan,
      surface: forced ? 'guard' : 'edit',
    });
    setBusy(true);
    return trackMutation(async (latest, settlement) => {
      try {
        const answer = await modelsApi.patchSource(
          latest.id,
          plan ? { ...patch, ...confirmGuardPlan(plan) } : patch,
        );
        const impact = committedPlan(answer.removed_hops, answer.interrupted);
        await commitManagementMutation(
          'edit',
          impact,
          (scope) => settlement.source(answer.source, scope),
        );
      } catch (error) {
        const failure = apiFailure(error);
        if (failure?.code === 'source_not_found') {
          await commitManagementMutation(
            'edit',
            null,
            (scope) => settlement.gone(latest.id, undefined, scope),
          );
        } else if (classifyModelHubFailure(failure) === 'inconclusive') {
          await reconcileEditWrite(latest, draft, patch, plan, settlement);
        } else {
          const refusal = guardedFailure(error);
          if (refusal) {
            dispatchManageStage({ type: 'guard_edit', draft, patch, plan: refusal });
            settlement.release();
          } else {
            dispatchManageStage({
              type: 'fail_edit',
              draft,
              patch,
              plan,
              forced,
              retryRead: false,
              before: latest,
            });
            settlement.release();
          }
        }
      }
    }).finally(() => setBusy(false));
  };
  const submitDelete = (plan: ManageGuardPlan | null) => {
    if (busy) return Promise.resolve();
    const forced = plan !== null;
    dispatchManageStage({ type: 'submit_delete', plan });
    setBusy(true);
    return trackMutation(async (latest, settlement) => {
      try {
        const answer = await modelsApi.deleteSource(
          latest.id,
          plan ? confirmGuardPlan(plan) : undefined,
        );
        const impact = committedPlan(answer.removed_hops, answer.interrupted);
        await commitManagementMutation(
          'delete',
          impact,
          (scope) => settlement.gone(latest.id, undefined, scope),
        );
      } catch (error) {
        const failure = apiFailure(error);
        if (failure?.code === 'source_not_found') {
          await commitManagementMutation(
            'delete',
            null,
            (scope) => settlement.gone(latest.id, undefined, scope),
          );
        } else if (classifyModelHubFailure(failure) === 'inconclusive') {
          await reconcileDeleteWrite(latest, plan, settlement);
        } else {
          const refusal = guardedFailure(error);
          if (refusal) {
            dispatchManageStage({ type: 'guard_delete', plan: refusal });
            settlement.release();
          } else {
            dispatchManageStage({ type: 'fail_delete', plan, forced, retryRead: false, before: latest });
            settlement.release();
          }
        }
      }
    }).finally(() => setBusy(false));
  };
  const retryEditRead = (failed: Extract<ManageStage, { kind: 'edit_failed' }>) => {
    if (busy) return Promise.resolve();
    dispatchManageStage({ type: 'retry' });
    setBusy(true);
    return trackMutation(async (_latest, settlement) => {
      await reconcileEditWrite(
        failed.before,
        failed.draft,
        failed.patch,
        failed.plan,
        settlement,
      );
    }).finally(() => setBusy(false));
  };
  const retryDeleteRead = (failed: Extract<ManageStage, { kind: 'delete_failed' }>) => {
    if (busy) return Promise.resolve();
    dispatchManageStage({ type: 'retry' });
    setBusy(true);
    return trackMutation(async (_latest, settlement) => {
      await reconcileDeleteWrite(failed.before, failed.plan, settlement);
    }).finally(() => setBusy(false));
  };
  const submitEditDialog = () => {
    if (!editDraft || !editAssessment.valid || !editAssessment.patch) return;
    if (manageStage.kind === 'edit_failed' && manageStage.retryRead) {
      void retryEditRead(manageStage);
      return;
    }
    const retryPlan = manageStage.kind === 'edit_failed' && manageStage.forced
      ? manageStage.plan
      : null;
    void submitEdit(editDraft, editAssessment.patch, retryPlan);
  };
  const retryDelete = () => {
    if (manageStage.kind !== 'delete_failed') return;
    if (manageStage.retryRead) {
      void retryDeleteRead(manageStage);
      return;
    }
    void submitDelete(manageStage.forced ? manageStage.plan : null);
  };
  const cancelManage = () => {
    if (busy) return;
    dispatchManageStage({ type: 'cancel' });
  };
  const dismissUnresolvedManage = () => {
    if (busy) return;
    dispatchManageStage({ type: 'dismiss_unresolved' });
  };
  const editManageDraft = (draft: SourceEditDraft) => {
    dispatchManageStage({ type: 'edit_draft', draft });
  };
  const confirmManage = () => {
    if (manageStage.kind === 'confirming_edit') {
      void submitEdit(manageStage.draft, manageStage.patch, manageStage.plan);
    }
    if (manageStage.kind === 'confirming_delete') void submitDelete(manageStage.plan);
  };
  const confirmGuard = () => {
    if (!guard) return;
    if (guard.kind === 'refetch') void refetch(confirmGuardPlan(guard.plan));
    if (guard.kind === 'removeModel') void remove(guard.model, confirmGuardPlan(guard.plan));
  };
  const adoptedBy = activeSourceAdoption(source.adopted_by, activeBackends);
  const adoptedBackends = [...new Set((adoptedBy ?? []).map(({ backend }) => t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string))];
  const state = sourceStatePresentation(source.state, 'detail', i18n.language, now, {
    known: adoptedBy !== undefined,
    backends: adoptedBackends,
    native: source.supply_channel === 'native_cli',
    verificationPending: Boolean(source.verification_pending),
  });
  // One authority picks the remedy and another total Record names its concrete
  // control. In particular, `retest` points at the existing refetch button; it is
  // not a special case that can fall out of destination-completeness checks.
  const repair = repairAction(source);
  const repairDestination = repair ? REPAIR_DESTINATION[repair] : null;
  const editDialogOpen = manageStage.kind === 'editing'
    || manageStage.kind === 'edit_failed'
    || (manageStage.kind === 'submitting_edit' && manageStage.surface === 'edit');
  const manageGuardOpen = manageStage.kind === 'confirming_edit'
    || manageStage.kind === 'confirming_delete'
    || manageStage.kind === 'submitting_delete'
    || (manageStage.kind === 'submitting_edit' && manageStage.surface === 'guard');
  const manageUnresolved = (manageStage.kind === 'edit_failed' || manageStage.kind === 'delete_failed')
    && manageStage.retryRead;
  const managePlan = 'plan' in manageStage ? manageStage.plan : null;
  const manageCopyKind = manageStage.kind.includes('edit') ? 'editSource' : 'deleteSource';
  const providerIdentity = sourceProviderIdentity(source);
  const providerCopyKey = SOURCE_PROVIDER_COPY_KEYS[providerIdentity];
  const providerLabel = providerCopyKey ? t(providerCopyKey) : providerIdentity;
  const interfaceLabel = `${providerLabel} · ${t(PROTOCOL_COPY_KEYS[source.protocol])}`;
  const credentialLabel = source.kind === 'api_key'
    ? t('settings.models.sourceDetail.metadata.apiKey')
    : t('settings.models.sourceDetail.metadata.account');
  const credentialValue = source.kind === 'api_key'
    ? source.masked_credential ?? '—'
    : source.account_label ?? '—';
  const endpoint = source.base_url ?? t('settings.models.sourceDetail.metadata.officialEndpoint');
  const lastFetched = source.last_discovered_at
    ? formatRelativeTime(source.last_discovered_at, t)
    : t('settings.models.sourceDetail.metadata.neverFetched');
  const usageSummary = adoptedBackends.length > 0
    ? t('settings.models.sourceDetail.usageSummary', { count: source.models.length, backends: adoptedBackends.join(i18n.language.startsWith('zh') ? '、' : ', ') })
    : t('settings.models.gateway.modelCount', { count: source.models.length });

  return (
    <div className="model-hub-source-detail">
      <section className="model-hub-source-bar flex shrink-0 items-center border-b border-border bg-surface">
        <span className={cn('model-hub-source-tile flex shrink-0 items-center justify-center', ACCENT_TILE[accent])}><Icon className={cn('size-[18px]', ACCENT_ICON[accent])} /></span>
        <div className="model-hub-source-copy flex min-w-0 flex-1 flex-col">
          <span className="model-hub-source-eyebrow">{t('settings.models.upstream.heading')}</span>
          <div className="model-hub-source-line flex min-w-0 flex-wrap items-center gap-x-[7px]">
            <h2 ref={headingRef} tabIndex={-1} className="model-hub-source-title truncate font-bold text-foreground">{source.display_name}</h2>
            {state.key && <span className={cn('model-hub-source-state model-hub-pill flex items-center gap-1.5 border', state.textClass)}><span className={cn('size-[5px] shrink-0 rounded-full', state.dotClass)} />{t(state.key, state.values)}{state.hint && <ModelHubInfoHint label={t(state.hint.labelKey) as string} content={t(state.hint.bodyKey)} className="size-[13px]" />}</span>}
          </div>
          <p className="model-hub-source-summary truncate">{usageSummary}</p>
        </div>
      </section>
      <dl className="model-hub-source-metadata grid shrink-0 grid-cols-2 gap-x-5 gap-y-3 border-b border-border bg-background px-5 py-3 sm:grid-cols-4">
        <div className="min-w-0"><dt>{t('settings.models.sourceDetail.metadata.endpoint')}</dt><dd className="truncate font-mono" title={endpoint as string}>{endpoint}</dd></div>
        <div className="min-w-0"><dt>{credentialLabel}</dt><dd className="truncate font-mono" title={credentialValue}>{credentialValue}</dd></div>
        <div className="min-w-0"><dt>{t('settings.models.sourceDetail.metadata.type')}</dt><dd className="truncate" title={interfaceLabel}>{interfaceLabel}</dd></div>
        <div className="min-w-0"><dt>{t('settings.models.sourceDetail.metadata.lastFetched')}</dt><dd className="truncate">{lastFetched}</dd></div>
      </dl>
      {(result || refetchFailed || manageStage.kind === 'delete_failed') && <div className="model-hub-source-notices shrink-0 space-y-2 px-5 pt-3">
        {result && result.removed.length > 0 && <p className="model-hub-status-gold rounded-lg border px-3 py-2 text-[11.5px]">{t('settings.models.sourceDetail.refetch.removed', { count: result.removed.length, models: result.removed.join(', ') })}</p>}
        {result && result.added.length === 0 && result.removed.length === 0 && <p className="model-hub-status-mint rounded-lg border px-3 py-2 text-[11.5px]">{t('settings.models.sourceDetail.refetch.unchangedOnly')}</p>}
        {refetchFailed && <p className="rounded-lg border border-destructive/25 bg-destructive/[0.08] px-3 py-2 text-[11.5px] text-destructive-ink">{t('settings.models.sourceDetail.fail.refetch')}</p>}
        {manageStage.kind === 'delete_failed' && <p data-manage-failure="delete" data-manage-retry-read={String(manageStage.retryRead)} className="rounded-lg border border-destructive/25 bg-destructive/[0.08] px-3 py-2 text-[11.5px] text-destructive-ink">{t(manageStage.retryRead ? 'settings.models.sourceDetail.fail.verifyDeleteSource' : 'settings.models.sourceDetail.fail.deleteSource')} <button type="button" disabled={busy} onClick={retryDelete} className="font-semibold underline underline-offset-2">{t('settings.models.sourceDetail.retry')}</button> <button type="button" disabled={busy} onClick={manageStage.retryRead ? dismissUnresolvedManage : cancelManage} className="font-semibold underline underline-offset-2">{t(manageStage.retryRead ? 'settings.models.sourceDetail.dismissUnverified' : 'common.cancel')}</button></p>}
      </div>}
      <section className="model-hub-source-toolbar flex shrink-0 flex-col gap-2 border-b border-border px-5 py-2.5 sm:flex-row sm:items-center">
        <span className="flex shrink-0 items-center gap-2"><h3 className="text-[13px] font-bold text-foreground">{t('settings.models.sourceDetail.modelsHeading')}</h3><span className="model-hub-pill model-hub-fill-0a border border-border text-muted">{models.length}</span></span>
        <div className="relative min-w-0 flex-1 sm:ml-auto sm:max-w-[200px]">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted" aria-hidden="true" />
          <Input value={query} onChange={(event) => setQuery(event.target.value)} aria-label={t('settings.models.sourceDetail.search') as string} placeholder={t('settings.models.sourceDetail.search') as string} className="model-hub-source-search h-8 pl-8 text-[11.5px]" />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {repairDestination === 'reauth_dialog' && <Button size="xs" className="model-hub-source-action" data-repair-kind={repair} data-repair-destination={repairDestination} disabled={busy} onClick={() => setConfirmingReauth(true)}><LogIn />{t(REPAIR_LABEL_KEY.reauth)}</Button>}
          {repairDestination === 'replace_key_dialog' && <Button size="xs" className="model-hub-source-action" data-repair-kind={repair} data-repair-destination={repairDestination} disabled={busy} onClick={() => setReplacingKey(true)}>{t(REPAIR_LABEL_KEY.replace_key)}</Button>}
          {source.supply_channel === 'hub' && <Button variant="outline" size="xs" className="model-hub-source-action" data-repair-kind={repairDestination === 'refetch_button' ? repair : undefined} data-repair-destination={repairDestination === 'refetch_button' ? repairDestination : undefined} disabled={busy} onClick={() => void refetch()}>{busy ? <Loader2 className="animate-spin" /> : <RefreshCw />}{t('settings.models.sourceDetail.action.refetch')}</Button>}
          {source.kind === 'api_key' && <Button size="xs" className="model-hub-source-action" disabled={busy || manualDraft !== null} onClick={() => setManualDraft({ modelId: '', tiers: [], failed: false, retryRead: false })}><Plus />{t('settings.models.sourceDetail.action.addModel')}</Button>}
          <SourceManageMenu source={source} busy={busy || manageStage.kind !== 'idle'} onEdit={beginEdit} onDelete={beginDelete} />
        </div>
      </section>
      <section className="model-hub-source-table flex min-h-0 flex-1 flex-col overflow-hidden bg-surface">
        <div className="model-hub-source-table-head hidden border-b border-border font-semibold md:grid">
          <span className="truncate">{t('settings.models.sourceDetail.col.id')}</span><span className="flex min-w-0 items-center gap-1"><span className="truncate">{t('settings.models.sourceDetail.col.tiers')}</span><Info className="model-hub-ink-59 size-[13px] shrink-0" /></span><span />
        </div>
        <div className="model-hub-source-table-scroll min-h-0 flex-1 overflow-y-auto">
        {filteredModels.length === 0 && !manualDraft && <p className="px-5 py-12 text-center text-[12px] text-muted">{t(normalizedQuery ? 'settings.models.sourceDetail.searchEmpty' : source.last_discovered_at ? 'settings.models.sourceDetail.empty' : 'settings.models.sourceDetail.emptyNeverFetched')}</p>}
        {models.map((model) => (
          <div key={model.id} hidden={!visibleModelIds.has(model.id)} className="model-hub-source-table-row grid gap-3 border-b border-border last:border-b-0 md:items-center md:gap-y-0">
            <span className="flex min-w-0 items-center gap-2"><span className="model-hub-source-model truncate font-mono text-foreground" title={model.id}>{model.id}</span>{model.origin !== 'discovered' && <span className="model-hub-source-pill model-hub-source-entry-pill model-hub-source-entry-pill--manual w-fit rounded-full border font-semibold">{t('settings.models.sourceDetail.entry.manual')}</span>}{result?.added.includes(model.id) && <span className="model-hub-accent-pill--mint model-hub-source-pill rounded-full border px-2 py-0.5 font-semibold">{t('settings.models.sourceDetail.refetch.added')}</span>}</span>
            <TierEditor
              model={model}
              protocol={source.protocol}
              editing={editingTiers === model.id}
              onEdit={() => setEditingTiers(model.id)}
              // Guarded against the row it is closing: the outgoing editor's blur
              // lands after the incoming row's click, so an unguarded close would
              // collapse the editor the user just opened.
              onClose={() => setEditingTiers((current) => (current === model.id ? null : current))}
              onMutating={() => { setResult(null); setRefetchFailed(false); }}
              trackMutation={trackMutation}
            />
            <div className="model-hub-source-row-actions flex items-center justify-end gap-1">
              {removeFailure?.modelId === model.id && <span className="model-hub-source-tier text-right text-destructive-ink">{t('settings.models.sourceDetail.fail.removeModel')} <button type="button" disabled={busy} onClick={() => {
                if (!removeFailure.retryRead) void remove(model);
                else {
                  setBusy(true);
                  void trackMutation(async (latest, settlement) => reconcileRemoval(latest.id, model, settlement))
                    .finally(() => setBusy(false));
                }
              }} className="font-semibold underline underline-offset-2">{t('settings.models.sourceDetail.retry')}</button></span>}
              {/* The pencil is a second door into the same tier editor, so a locked
                  row has to close it too — a manual entry whose id matches a
                  catalog model is locked exactly like a discovered one. Removing
                  the model itself is a different question and stays offered. */}
              {model.origin === 'manual' && <>{!managedTierSource(model.reasoning_efforts_source) && <button type="button" disabled={busy} aria-label={t('settings.models.sourceDetail.row.edit', { model: model.id }) as string} title={t('settings.models.sourceDetail.row.edit', { model: model.id }) as string} className="model-hub-source-row-action grid place-items-center text-muted hover:bg-surface-2 hover:text-foreground" onClick={() => setEditingTiers(model.id)}><Pencil className="size-3.5" /></button>}<ManualModelMenu model={model} busy={busy} onRemove={() => void remove(model)} /></>}
            </div>
          </div>
        ))}
        {manualDraft && (
          <div
            data-manual-model-draft
            data-source-dialog-local-escape
            className="model-hub-source-table-draft grid gap-3 border-y border-mint/20 bg-mint/[0.05] md:items-center"
            onKeyDown={(event) => {
              if (event.key !== 'Escape') return;
              event.preventDefault();
              if (!busy) setManualDraft(null);
            }}
          >
            <span className="model-hub-source-draft-line flex min-w-0 items-center gap-2"><Input
                autoFocus
                value={manualDraft.modelId}
                onChange={(event) => setManualDraft((current) => current ? { ...current, modelId: event.target.value, failed: false, retryRead: false } : current)}
                placeholder={t('settings.models.sourceDetail.col.id') as string}
                className="model-hub-source-manual-id model-hub-source-model h-8 min-w-0 font-mono"
              /><span className="model-hub-source-pill model-hub-source-entry-pill model-hub-source-entry-pill--manual w-fit rounded-full border font-semibold">{t('settings.models.sourceDetail.entry.manual')}</span></span>
            <DraftTiers tiers={manualDraft.tiers} onChange={(tiers) => setManualDraft((current) => current ? { ...current, tiers, failed: false, retryRead: false } : current)} />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="xs" disabled={busy} onClick={() => setManualDraft(null)}>{t('common.cancel')}</Button>
              <Button size="xs" disabled={busy || !manualDraft.modelId.trim() || source.models.some((model) => model.id === manualDraft.modelId.trim())} onClick={() => void addManualModel()}>{manualDraft.failed ? t('settings.models.sourceDetail.retry') : t('settings.models.sourceDetail.action.addModel')}</Button>
            </div>
            {manualDraft.failed && <p className="model-hub-source-draft-line text-[11px] text-destructive-ink">{t('settings.models.sourceDetail.fail.addModel')}</p>}
          </div>
        )}
        </div>
      </section>
      {/* Confirmed before the journey opens, not inside it: `OAuthConnectDialog`
          POSTs the re-auth as it mounts, and on a native source that call is the
          irreversible half — `mark_native_irreversible_start` empties every
          sibling source on the shared CLI. `reauthBodyKey` is what makes one
          confirm true on both channels; a single sentence over both warns a hub
          user about a loss that does not happen and stays silent about the one
          that does. */}
      <ConfirmDialog
        open={confirmingReauth}
        onOpenChange={setConfirmingReauth}
        title={t('settings.models.repair.reauthTitle', { name: source.display_name })}
        description={t(reauthBodyKey(source))}
        confirmLabel={t('settings.models.repair.reauthConfirm') as string}
        destructive={reauthCost(source) === 'immediate'}
        onConfirm={() => {
          // This click is the journey's only user gesture: the dialog it opens POSTs
          // as it mounts, and a browser grants the provider tab to the gesture that
          // asked for it, not to the response one round trip later. Allocate here and
          // hand it over, or the provider handoff is popup-blocked and the user has
          // to notice the fallback link.
          handOffProviderTab();
          setConfirmingReauth(false);
          onReauth(source);
        }}
      />
      <AddApiKeyDialog
        mode="replace"
        open={replacingKey}
        source={source}
        trackMutation={trackMutation}
        onClose={() => setReplacingKey(false)}
      />
      <DialogPrimitive.Root
        open={editDialogOpen}
        onOpenChange={(open) => { if (!open) cancelManage(); }}
      >
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="model-hub-guard-overlay fixed inset-0 z-50" />
          <DialogPrimitive.Content
            className="model-hub-guard-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto border border-border-strong bg-surface outline-none"
            onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
            onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
          >
            <form className="contents" onSubmit={(event) => {
              event.preventDefault();
              submitEditDialog();
            }}>
              <header className="model-hub-guard-head">
                <div className="flex items-center justify-between gap-3">
                  <DialogPrimitive.Title className="model-hub-guard-title text-foreground">{t('settings.models.sourceDetail.edit.title', { source: source.display_name })}</DialogPrimitive.Title>
                  <DialogPrimitive.Close asChild><Button type="button" variant="ghost" size="icon" className="model-hub-guard-close" disabled={busy || manageUnresolved} aria-label={t('settings.models.sourceDetail.edit.cancel')} title={t('settings.models.sourceDetail.edit.cancel')}><X aria-hidden /></Button></DialogPrimitive.Close>
                </div>
                <DialogPrimitive.Description className="model-hub-guard-subtitle">{t(`settings.models.sourceKind.${source.kind}`)} · {t(PROTOCOL_COPY_KEYS[source.protocol])}</DialogPrimitive.Description>
              </header>
              {editDraft && <div className="model-hub-guard-body">
                <Field label={t('settings.models.sourceDetail.edit.name')}>
                  {(id) => <Input id={id} autoFocus value={editDraft.displayName} disabled={busy} aria-invalid={editAssessment.reason?.startsWith('displayName') || undefined} onChange={(event) => editManageDraft({ ...editDraft, displayName: event.target.value })} />}
                </Field>
                {canEditSourceEndpoint(source) && <Field label={t('settings.models.sourceDetail.edit.baseUrl')} mono>
                  {(id) => <Input id={id} type="text" inputMode="url" autoComplete="url" spellCheck={false} value={editDraft.baseUrl} disabled={busy} aria-invalid={editAssessment.reason?.startsWith('baseUrl') || undefined} className="font-mono" onChange={(event) => editManageDraft({ ...editDraft, baseUrl: event.target.value })} />}
                </Field>}
                <p className="model-hub-guard-hint"><Info aria-hidden />{t('settings.models.sourceDetail.edit.hint')}</p>
                {editAssessment.reason && <p data-source-edit-validation className="text-[11.5px] text-destructive-ink">{t(SOURCE_EDIT_REASON_KEY[editAssessment.reason])}</p>}
                {manageStage.kind === 'edit_failed' && <p data-manage-failure="edit" data-manage-retry-read={String(manageStage.retryRead)} className="text-[11.5px] text-destructive-ink">{t(manageStage.retryRead ? 'settings.models.sourceDetail.edit.verifyFail' : 'settings.models.sourceDetail.edit.fail')}</p>}
              </div>}
              <footer className="model-hub-guard-foot">
                <Button type="button" variant="outline" className="model-hub-guard-action" onClick={manageUnresolved ? dismissUnresolvedManage : cancelManage} disabled={busy}>{t(manageUnresolved ? 'settings.models.sourceDetail.dismissUnverified' : 'settings.models.sourceDetail.edit.cancel')}</Button>
                <Button type="submit" className="model-hub-guard-action" disabled={busy || !editAssessment.valid || !editAssessment.patch}>{busy && <Loader2 className="animate-spin" />}{t(busy ? 'settings.models.sourceDetail.edit.saving' : manageStage.kind === 'edit_failed' && manageStage.retryRead ? 'settings.models.sourceDetail.retry' : 'settings.models.sourceDetail.edit.save')}</Button>
              </footer>
            </form>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
      <DialogPrimitive.Root open={guard !== null || manageGuardOpen} onOpenChange={(open) => {
        if (!open && !busy) {
          if (manageGuardOpen) cancelManage();
          else setGuard(null);
        }
      }}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="model-hub-guard-overlay fixed inset-0 z-50" />
          <DialogPrimitive.Content
            className="model-hub-guard-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto border border-border-strong bg-surface outline-none"
            onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
            onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
          >
            <header className="model-hub-guard-head">
              <div className="flex items-center justify-between gap-3">
                <DialogPrimitive.Title className="model-hub-guard-title text-foreground">{manageGuardOpen
                    ? t(`settings.models.guard.title.${manageCopyKind}`, { source: source.display_name })
                    : t(`settings.models.guard.title.${guard ? GUARD_COPY_KIND[guard.kind] : 'refetch'}`, { model: guard?.kind === 'removeModel' ? guard.model.id : undefined, source: source.display_name })}</DialogPrimitive.Title>
                <DialogPrimitive.Close asChild><Button type="button" variant="ghost" size="icon" className="model-hub-guard-close" disabled={busy} aria-label={t('settings.models.guard.cancel')} title={t('settings.models.guard.cancel')}><X aria-hidden /></Button></DialogPrimitive.Close>
              </div>
              <DialogPrimitive.Description className="model-hub-guard-subtitle">{manageGuardOpen
                  ? t(`settings.models.guard.subtitle.${manageCopyKind}`)
                  : t(`settings.models.guard.subtitle.${guard ? GUARD_COPY_KIND[guard.kind] : 'refetch'}`)}</DialogPrimitive.Description>
            </header>
            {manageGuardOpen && managePlan && <div className="model-hub-guard-body"><GuardImpact hops={managePlan.hops} gaps={managePlan.gaps} /></div>}
            {!manageGuardOpen && guard?.plan && <div className="model-hub-guard-body"><GuardImpact hops={guard.plan.hops} gaps={guard.plan.gaps} /></div>}
            {manageGuardOpen
              ? <footer className="model-hub-guard-foot"><Button variant="outline" className="model-hub-guard-action" onClick={cancelManage} disabled={busy}>{t('settings.models.guard.cancel')}</Button><Button variant="destructive" className="model-hub-guard-action" onClick={confirmManage} disabled={busy}>{busy && <Loader2 className="animate-spin" />}{t(`settings.models.guard.confirm.${manageCopyKind}`)}</Button></footer>
              : <footer className="model-hub-guard-foot"><Button variant="outline" className="model-hub-guard-action" onClick={() => setGuard(null)} disabled={busy}>{t('settings.models.guard.cancel')}</Button><Button variant="destructive" className="model-hub-guard-action" onClick={confirmGuard} disabled={busy}>{busy && <Loader2 className="animate-spin" />}{t(`settings.models.guard.confirm.${guard ? GUARD_COPY_KIND[guard.kind] : 'refetch'}`)}</Button></footer>}
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </div>
  );
};
