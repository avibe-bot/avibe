import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import {
  Bot,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileText,
  Funnel,
  Lock,
  LockKeyhole,
  Loader2,
  Maximize2,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Star,
  Trash2,
  Upload,
  Activity,
  Layers,
} from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import type {
  VibeAgentBrief,
  VibeAgentFull,
  VibeAgentOnboardingResult,
  VibeAgentUpdatePayload,
} from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { AgentGraphTab } from './AgentGraphTab';
import { useToast } from '../../context/ToastContext';
import { NewAgentDialog } from './NewAgentDialog';
import { RunAgentDialog } from './RunAgentDialog';
import { GlobalPromptsDialog } from './GlobalPromptsDialog';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Switch } from '../ui/switch';
import { Combobox } from '../ui/combobox';
import type { ComboboxOption } from '../ui/combobox';
import { Textarea } from '../ui/textarea';
import { EditorDialog } from '../ui/editor-dialog';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { onPageReactivated } from '../../lib/pageActivity';
import { estimateTokens } from '../../lib/tokenEstimate';
import { loadBackendModelsWithRefresh, modelOptionLabel } from '../../lib/backendModels';
import { resolveEffortOptions } from '../../lib/effortOptions';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { CapabilityTabs } from './CapabilityTabs';
// Backend order / labels / accent classes live in lib/backendAccent, shared
// with the Skills surface (BACKEND_TEXT is this page's old BACKEND_ICON_CLASS).
import {
  BACKEND_ORDER,
  BACKEND_LABEL,
  BACKEND_TEXT as BACKEND_ICON_CLASS,
  type Backend,
} from '../../lib/backendAccent';
import { errorMessage } from '@/lib/errorMessage';
// Tab set + its cross-visit memory live together so the remembered value can
// never name a tab this page no longer renders (see agentsViewMemory).
import {
  AGENTS_TAB_ORDER,
  resolveAgentsTab,
  writeAgentsTab,
  type AgentsTabKey,
} from '../../lib/agentsViewMemory';

function isSystemAgent(agent: { source: string }): boolean {
  return agent.source === 'builtin' || agent.source === 'system';
}

type AgentRequestToken = { version: number; identity: string | null };

type AgentRequestVersion = {
  begin: (identity?: string | null) => AgentRequestToken;
  invalidate: () => number;
  current: () => number;
  isCurrent: (token: AgentRequestToken) => boolean;
};

type SelectedMutationBatch = {
  id: number;
  identity: string;
  pending: number;
  version: number;
  operations: SelectedMutationOperation[];
};

type SelectedMutationResult = { ok: true } | { ok: false; error: unknown };

type SelectedMutationOperation = {
  id: number;
  batchId: number;
  identity: string;
  sequence: number;
  failure?: unknown;
  joinedDetailBarrier?: boolean;
  resolve?: (result: SelectedMutationResult) => void;
};

type SelectedReconciliationWaiter = {
  resolve: (result: SelectedMutationResult) => void;
};

type SelectedReconciliationDebt = {
  version: number;
  scheduled: boolean;
  waiters: SelectedReconciliationWaiter[];
};

type AutoSelectReason = 'initial' | 'replacement';
type SelectedReadPurpose = 'selection' | 'reconcile' | 'continuation' | 'debt';

type SelectedReadStage = {
  key: string;
  identity: string;
  identityEpoch: number;
  causalFloor: number;
  intentGeneration: number;
  readGeneration: number;
  debtVersion?: number;
  obligationId: number;
  purpose: SelectedReadPurpose;
  expectedCodes?: readonly string[];
  debtOnly: boolean;
  refreshDefinitions: boolean;
  rollbackOnFailure: boolean;
  clearSelectionError: boolean;
  invalidated: boolean;
  promise: Promise<SelectedReadOutcome>;
  resolve: (outcome: SelectedReadOutcome) => void;
  settled: boolean;
};

type SelectedRetirement = {
  retired: boolean;
  resumedIdentity: string | null;
  intentGeneration: number;
};

type SelectedReadContinuation = { nextIdentity: string; intentGeneration: number };

type SelectedReadOutcome =
  | { kind: 'published'; continuation?: SelectedReadContinuation }
  | { kind: 'failed'; error?: unknown; continuation?: SelectedReadContinuation }
  | { kind: 'stale'; continuation?: SelectedReadContinuation }
  | { kind: 'expected-retired'; continuation?: SelectedReadContinuation };

type SelectedCoordinator = {
  desiredName: string | null;
  desiredOpenDetail: boolean;
  desiredSource: 'user' | 'auto' | 'passive';
  identityEpoch: number;
  intentGeneration: number;
  readGenerations: Map<string, number>;
  accepted: VibeAgentFull | null;
  acceptedGeneration: number;
  stages: Map<string, SelectedReadStage>;
  stageQueue: SelectedReadStage[];
  stageDrainActive: boolean;
  obligationAttempts: Map<number, Set<string>>;
  nextObligationId: number;
  nextBatchId: number;
  nextOperationId: number;
  nextMutationVersion: number;
  mutations: Map<string, SelectedMutationBatch>;
  reconciliationDebt: Map<string, SelectedReconciliationDebt>;
  retired: Set<string>;
  retiredAtDefinitionsVersion: Map<string, number>;
  autoSelectReason: AutoSelectReason | null;
  autoSelectDismissed: boolean;
};

type DefinitionsBarrierWaiter = {
  watermark: number;
  resolve: (published: boolean) => void;
};

type DefinitionsBarrierState = {
  publishedVersion: number;
  waiters: DefinitionsBarrierWaiter[];
};

const createSelectedCoordinator = (): SelectedCoordinator => ({
  desiredName: null,
  desiredOpenDetail: false,
  desiredSource: 'passive',
  identityEpoch: 0,
  intentGeneration: 0,
  readGenerations: new Map(),
  accepted: null,
  acceptedGeneration: 0,
  stages: new Map(),
  stageQueue: [],
  stageDrainActive: false,
  obligationAttempts: new Map(),
  nextObligationId: 0,
  nextBatchId: 0,
  nextOperationId: 0,
  nextMutationVersion: 0,
  mutations: new Map(),
  reconciliationDebt: new Map(),
  retired: new Set(),
  retiredAtDefinitionsVersion: new Map(),
  autoSelectReason: 'initial',
  autoSelectDismissed: false,
});

const advanceSelectedRead = (coordinator: SelectedCoordinator, identity: string): number => {
  const next = (coordinator.readGenerations.get(identity) ?? 0) + 1;
  coordinator.readGenerations.set(identity, next);
  for (const stage of coordinator.stages.values()) {
    if (stage.identity === identity && stage.readGeneration < next) stage.invalidated = true;
  }
  return next;
};

const selectedReadIsCurrent = (
  coordinator: SelectedCoordinator,
  identity: string,
  generation: number,
): boolean => coordinator.readGenerations.get(identity) === generation;

const releaseSelectedDebtWaiters = (coordinator: SelectedCoordinator, identity: string) => {
  const debt = coordinator.reconciliationDebt.get(identity);
  if (!debt || debt.waiters.length === 0) return;
  // Leaving an identity supersedes its in-flight read. The mutation itself is
  // already terminal, so settle callers now; any retained debt is evidence for
  // the next authoritative read when the identity is selected again.
  debt.scheduled = false;
  const waiters = debt.waiters.splice(0);
  for (const waiter of waiters) {
    waiter.resolve({ ok: true });
  }
};

type ResourceErrorOwner = 'definitions' | 'selection' | 'mutation';
type ResourceErrors = Record<ResourceErrorOwner, string | null>;

// A tiny per-resource epoch owner. Every asynchronous read captures a token;
// any committed mutation or identity change advances the epoch and makes older
// responses inert without relying on timing or a name comparison alone.
const createAgentRequestVersion = (): AgentRequestVersion => {
  let version = 0;
  return {
    begin: (identity = null) => ({ version: ++version, identity }),
    invalidate: () => ++version,
    current: () => version,
    isCurrent: (token) => token.version === version,
  };
};

const SELECTED_DISAPPEARANCE_CODES = ['agent_not_found', 'agent_access_forbidden'] as const;

const errorCodeOf = (error: unknown): string | null => {
  if (!error || typeof error !== 'object') return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' ? code : null;
};

export const AgentsPage: React.FC = () => {
  const { t } = useTranslation();
  const tRef = useRef(t);
  tRef.current = t;
  const api = useApi();
  const { showToast } = useToast();
  const {
    capabilities,
  } = useInstanceAuthorization();
  // General navigation (sidebar / nav / capability tabs) resumes the tab the user
  // left the page on; a fresh browser opens Definitions. A contextual caller that
  // needs a specific tab passes ``?tab=`` and wins over the memory — the tab it
  // asked for is a destination, not a choice the user made, so it is deliberately
  // NOT written back to the memory.
  const [searchParams] = useSearchParams();
  const tabParam = searchParams.get('tab');
  const [agentsTab, setAgentsTab] = useState<AgentsTabKey>(() => resolveAgentsTab(tabParam));
  // One-way URL -> state, keyed on the param so a later user tab click isn't
  // yanked back: both a contextual link arriving while this page is already
  // mounted, and that param going away again (the sidebar link from a pinned URL
  // changes the URL without remounting) are param changes, and the second one is
  // bare navigation — back to the remembered tab.
  useEffect(() => {
    setAgentsTab(resolveAgentsTab(tabParam));
  }, [tabParam]);
  const selectAgentsTab = useCallback((next: AgentsTabKey) => {
    setAgentsTab(next);
    writeAgentsTab(next);
  }, []);
  const [runningActiveCount, setRunningActiveCount] = useState<number | null>(null);
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);
  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  const [defaultName, setDefaultName] = useState<string | null>(null);
  const [selected, setSelected] = useState<VibeAgentFull | null>(null);
  const [loading, setLoading] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [showGlobalPrompts, setShowGlobalPrompts] = useState(false);
  const [resourceErrors, setResourceErrors] = useState<ResourceErrors>({
    definitions: null,
    selection: null,
    mutation: null,
  });
  const resourceErrorGenerationRef = useRef<Record<ResourceErrorOwner, number>>({
    definitions: 0,
    selection: 0,
    mutation: 0,
  });
  const mutationErrorSequenceRef = useRef(0);
  const [search, setSearch] = useState('');
  const [backendFilter, setBackendFilter] = useState<Backend | 'all'>('all');
  const [importing, setImporting] = useState<Backend | null>(null);
  const [onboardingInventory, setOnboardingInventory] = useState<VibeAgentOnboardingResult | null>(null);
  const [onboardingExpanded, setOnboardingExpanded] = useState(false);
  const [onboardingSubmitting, setOnboardingSubmitting] = useState(false);
  const onboardingSettlementRef = useRef<Promise<boolean> | null>(null);
  const definitionsVersionRef = useRef(createAgentRequestVersion());
  const definitionsBarrierRef = useRef<DefinitionsBarrierState>({ publishedVersion: 0, waiters: [] });
  const onboardingVersionRef = useRef(createAgentRequestVersion());
  const selectedCoordinatorRef = useRef(createSelectedCoordinator());
  const selectedMountedRef = useRef(true);
  const catchUpEpochRef = useRef(0);
  // Mobile drill-down: a row tap opens the detail full-screen. The agent
  // auto-selected on mount stays in the list view until the user drills in.
  const [detailOpen, setDetailOpen] = useState(false);
  const visibleTabs = capabilities.can_use_agents ? AGENTS_TAB_ORDER : (['definitions'] as const);
  const activeTab = capabilities.can_use_agents ? agentsTab : 'definitions';
  const canEditAgents = capabilities.can_manage_agents;
  // Bulk Agent onboarding is a one-way instance-wide migration and stays Owner
  // on the HTTP policy, so it follows owner identity rather than the Agent-CRUD
  // capability a member also has: keying the fetch off `can_manage_agents` sent
  // a member's page load into an owner-only GET and surfaced the 403 as a toast.
  const canOnboardAgents = capabilities.is_instance_owner;

  const beginResourceError = useCallback((owner: ResourceErrorOwner) => {
    const generation = resourceErrorGenerationRef.current[owner] + 1;
    resourceErrorGenerationRef.current[owner] = generation;
    setResourceErrors((current) => (current[owner] === null ? current : { ...current, [owner]: null }));
    return generation;
  }, []);

  const setResourceError = useCallback((owner: ResourceErrorOwner, message: string, generation?: number) => {
    if (generation !== undefined && resourceErrorGenerationRef.current[owner] !== generation) return;
    setResourceErrors((current) => ({ ...current, [owner]: message }));
  }, []);

  const publishMutationError = useCallback(
    (value: unknown, sequence: number) => {
      if (mutationErrorSequenceRef.current > sequence) return;
      mutationErrorSequenceRef.current = sequence;
      setResourceError('mutation', errorMessage(value) || t('errorBoundary.title'));
    },
    [setResourceError, t],
  );

  const clearResourceError = useCallback((owner: ResourceErrorOwner, generation?: number) => {
    if (generation !== undefined && resourceErrorGenerationRef.current[owner] !== generation) return;
    resourceErrorGenerationRef.current[owner] += 1;
    setResourceErrors((current) => (current[owner] === null ? current : { ...current, [owner]: null }));
  }, []);

  const error = resourceErrors.mutation ?? resourceErrors.selection ?? resourceErrors.definitions;

  const publishOnboarding = useCallback((result: VibeAgentOnboardingResult | null) => {
    setOnboardingInventory(result?.available ? result : null);
  }, []);

  const refreshOnboarding = useCallback(() => {
    const request = (async () => {
      if (!selectedMountedRef.current) return false;
      if (!canOnboardAgents) {
        setOnboardingInventory(null);
        return true;
      }
      const token = onboardingVersionRef.current.begin();
      try {
        const result = await api.getVibeAgentOnboarding();
        if (selectedMountedRef.current && onboardingVersionRef.current.isCurrent(token)) {
          publishOnboarding(result);
          return true;
        }
      } catch {
        if (selectedMountedRef.current && onboardingVersionRef.current.isCurrent(token)) {
          publishOnboarding(null);
          return true;
        }
      }
      return false;
    })();
    onboardingSettlementRef.current = request;
    return request;
  }, [api, canOnboardAgents, publishOnboarding]);

  useEffect(() => {
    return () => {
      selectedMountedRef.current = false;
      definitionsVersionRef.current.invalidate();
      for (const waiter of definitionsBarrierRef.current.waiters.splice(0)) waiter.resolve(false);
      onboardingVersionRef.current.invalidate();
      const coordinator = selectedCoordinatorRef.current;
      for (const identity of coordinator.readGenerations.keys()) advanceSelectedRead(coordinator, identity);
      for (const stage of coordinator.stageQueue.splice(0)) {
        stage.invalidated = true;
        if (!stage.settled) {
          stage.settled = true;
          stage.resolve({ kind: 'stale' });
        }
      }
      coordinator.stages.clear();
      for (const batch of coordinator.mutations.values()) {
        for (const operation of batch.operations.splice(0)) {
          operation.resolve?.({ ok: false, error: new Error('Agent page unmounted') });
        }
      }
      for (const debt of coordinator.reconciliationDebt.values()) {
        for (const waiter of debt.waiters.splice(0)) waiter.resolve({ ok: false, error: new Error('Agent page unmounted') });
      }
      coordinator.reconciliationDebt.clear();
      coordinator.mutations.clear();
    };
  }, []);

  const scheduleSelectedReadRef = useRef<
    ((identity: string, options?: {
      expectedCodes?: readonly string[];
      debtOnly?: boolean;
      causalFloor?: number;
      refreshDefinitions?: boolean;
      rollbackOnFailure?: boolean;
      clearSelectionError?: boolean;
      purpose?: SelectedReadPurpose;
      obligationId?: number;
    }) => Promise<SelectedReadOutcome> | null)
  >(null);
  const drainSelectedStagesRef = useRef<(() => void) | null>(null);
  const retireSelectedIdentityRef = useRef<
    ((identity: string, options?: { refreshDefinitions?: boolean; cause?: unknown }) => SelectedRetirement) | null
  >(null);

  const commitSelected = useCallback((agent: VibeAgentFull | null) => {
    const coordinator = selectedCoordinatorRef.current;
    const previousIdentity = coordinator.accepted?.name;
    if (previousIdentity) advanceSelectedRead(coordinator, previousIdentity);
    if (agent?.name) advanceSelectedRead(coordinator, agent.name);
    coordinator.acceptedGeneration += 1;
    coordinator.accepted = agent;
    if (agent?.name) {
      coordinator.retired.delete(agent.name);
      coordinator.retiredAtDefinitionsVersion.delete(agent.name);
      if (coordinator.desiredSource === 'auto' && coordinator.desiredName === agent.name) {
        // Auto-selection is consumed only by an authoritative publication. A
        // failed attempt leaves the reason pending for the next external edge.
        coordinator.autoSelectReason = null;
      }
    }
    setSelected(agent);
    if (agent && coordinator.desiredName === agent.name && coordinator.desiredOpenDetail) {
      setDetailOpen(true);
    }
  }, []);

  const beginSelectedIntent = useCallback(
    (
      identity: string | null,
      options: { auto?: boolean; openDetail?: boolean; source?: 'user' | 'auto' | 'passive' } = {},
    ) => {
      const coordinator = selectedCoordinatorRef.current;
      const identityChanged = coordinator.desiredName !== identity;
      if (coordinator.desiredName && identityChanged) {
        releaseSelectedDebtWaiters(coordinator, coordinator.desiredName);
        advanceSelectedRead(coordinator, coordinator.desiredName);
      }
      if (identity && identityChanged) advanceSelectedRead(coordinator, identity);
      if (identity && !options.auto) {
        coordinator.retired.delete(identity);
        coordinator.retiredAtDefinitionsVersion.delete(identity);
        coordinator.autoSelectDismissed = false;
        coordinator.autoSelectReason = null;
      }
      if (identityChanged) coordinator.identityEpoch += 1;
      coordinator.intentGeneration += 1;
      coordinator.desiredName = identity;
      coordinator.desiredOpenDetail = Boolean(identity && options.openDetail);
      coordinator.desiredSource = options.source ?? (options.auto ? 'auto' : 'user');
      return { intentGeneration: coordinator.intentGeneration, identity };
    },
    [],
  );

  const rollbackSelectedIntent = useCallback(() => {
    const coordinator = selectedCoordinatorRef.current;
    if (coordinator.desiredName) advanceSelectedRead(coordinator, coordinator.desiredName);
    const acceptedIdentity = coordinator.accepted?.name ?? null;
    return beginSelectedIntent(
      acceptedIdentity && !coordinator.retired.has(acceptedIdentity) ? acceptedIdentity : null,
      { source: 'passive', openDetail: false },
    );
  }, [beginSelectedIntent]);


  // Definitions refreshes are explicit reconciliation reads. They must always
  // bypass the five-second client cache so a normal refresh cannot supersede a
  // reconnect snapshot with a stale cached promise.
  const refresh = useCallback(() => {
    const token = definitionsVersionRef.current.begin();
    const barrier = new Promise<boolean>((resolve) => {
      const state = definitionsBarrierRef.current;
      if (state.publishedVersion >= token.version) {
        resolve(true);
      } else {
        state.waiters.push({ watermark: token.version, resolve });
      }
    });
    const settleBarrier = (published: boolean) => {
      const state = definitionsBarrierRef.current;
      if (published) state.publishedVersion = Math.max(state.publishedVersion, token.version);
      const remaining: DefinitionsBarrierWaiter[] = [];
      for (const waiter of state.waiters) {
        if (waiter.watermark <= token.version) waiter.resolve(published);
        else remaining.push(waiter);
      }
      state.waiters = remaining;
    };
    void (async (): Promise<void> => {
      setLoading(true);
      clearResourceError('definitions');
      try {
        const result = await api.listVibeAgents({
          includeDisabled: true,
          cache: false,
        });
        // A read issued before a stream gap may finish after the catch-up read.
        // Only the latest request may publish its snapshot, so an older response
        // cannot roll the Definitions list back to pre-gap state.
        if (!definitionsVersionRef.current.isCurrent(token)) return;
        const coordinator = selectedCoordinatorRef.current;
        const visibleAgents = result.agents.filter((agent) => {
          const retiredAt = coordinator.retiredAtDefinitionsVersion.get(agent.name);
          if (retiredAt === undefined) return true;
          if (token.version > retiredAt) {
            coordinator.retired.delete(agent.name);
            coordinator.retiredAtDefinitionsVersion.delete(agent.name);
            return true;
          }
          return false;
        });
        setAgents(visibleAgents);
        setDefaultName(result.default_agent_name);
        // List omission is authoritative retirement evidence. The coordinator
        // owns all desired/accepted transitions; stale list responses cannot
        // re-select an identity after retirement because the retired set is
        // updated before the next render.
        const currentIdentities = [coordinator.accepted?.name, coordinator.desiredName].filter(
          (identity): identity is string => Boolean(identity),
        );
        const retired = currentIdentities.filter(
          (identity) => !result.agents.some((agent) => agent.name === identity) && !coordinator.mutations.has(identity),
        );
        if (retired.length > 0) {
          for (const identity of retired) {
            const retirement = retireSelectedIdentityRef.current?.(identity, { refreshDefinitions: false });
            if (retirement?.resumedIdentity) {
              const resumedDebt = coordinator.reconciliationDebt.get(retirement.resumedIdentity);
              scheduleSelectedReadRef.current?.(retirement.resumedIdentity, {
                expectedCodes: SELECTED_DISAPPEARANCE_CODES,
                debtOnly: Boolean(resumedDebt),
                rollbackOnFailure: false,
                clearSelectionError: false,
                purpose: 'continuation',
                obligationId: catchUpEpochRef.current || token.version,
                causalFloor: catchUpEpochRef.current || token.version,
              });
            }
          }
        }
        settleBarrier(true);
      } catch (err) {
        if (definitionsVersionRef.current.isCurrent(token)) {
          setResourceError('definitions', errorMessage(err) ?? String(err));
          settleBarrier(false);
        }
      } finally {
        if (definitionsVersionRef.current.isCurrent(token)) setLoading(false);
      }
    })();
    return barrier;
  }, [api, clearResourceError, setResourceError]);

  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  const retireSelectedIdentity = useCallback(
    (identity: string, options: { refreshDefinitions?: boolean; cause?: unknown } = {}) => {
      const coordinator = selectedCoordinatorRef.current;
      if (coordinator.retired.has(identity)) {
        return { retired: false, resumedIdentity: null, intentGeneration: coordinator.intentGeneration };
      }
      const acceptedIdentity = coordinator.accepted?.name ?? null;
      const pendingIdentity = coordinator.desiredName === identity;
      const acceptedCanResume = Boolean(
        acceptedIdentity &&
          acceptedIdentity !== identity &&
          !coordinator.retired.has(acceptedIdentity) &&
          !coordinator.autoSelectDismissed,
      );
      coordinator.retired.add(identity);
      coordinator.retiredAtDefinitionsVersion.set(identity, definitionsVersionRef.current.current());
      advanceSelectedRead(coordinator, identity);
      // A detail-level tombstone is Definitions evidence too. Remove the row
      // in the same transition, while the retirement watermark prevents an
      // older in-flight list response from resurrecting it.
      setAgents((current) => current.filter((agent) => agent.name !== identity));
      const debt = coordinator.reconciliationDebt.get(identity);
      coordinator.reconciliationDebt.delete(identity);
      for (const waiter of debt?.waiters.splice(0) ?? []) {
        waiter.resolve({ ok: false, error: options.cause ?? { code: 'agent_not_found' } });
      }
      if (pendingIdentity) {
        // A pending selection can disappear without invalidating the accepted
        // entity. Keep that accepted entity eligible for reconnect/debt
        // reconciliation instead of leaving visible A with no desired A.
        beginSelectedIntent(acceptedCanResume ? acceptedIdentity : null, {
          source: 'passive',
          openDetail: false,
        });
      }
      if (coordinator.accepted?.name === identity) {
        coordinator.desiredOpenDetail = false;
        coordinator.desiredSource = 'passive';
        commitSelected(null);
      }
      if (!coordinator.desiredName && !coordinator.accepted && !coordinator.autoSelectDismissed) {
        coordinator.autoSelectReason = 'replacement';
      }
      clearResourceError('selection');
      if (options.refreshDefinitions !== false) {
        // The causal follow-up itself must not resurrect the tombstone. Mark
        // the identity through the version that this refresh is about to
        // publish; a later external Definitions edge may reintroduce it.
        const followUpVersion = definitionsVersionRef.current.invalidate() + 1;
        coordinator.retiredAtDefinitionsVersion.set(identity, followUpVersion);
        void refreshRef.current();
      }
      return {
        retired: true,
        resumedIdentity: pendingIdentity && acceptedCanResume ? acceptedIdentity : null,
        intentGeneration: coordinator.intentGeneration,
      };
    },
    [beginSelectedIntent, clearResourceError, commitSelected],
  );
  retireSelectedIdentityRef.current = retireSelectedIdentity;

  // A successful rename keeps the stable entity id while changing the
  // transport key used by every read and mutation. Migrate all coordinator
  // state in one transition so an old-name response can never republish.
  const migrateSelectedIdentity = useCallback(
    (oldName: string, newName: string, stableId: string) => {
      if (!oldName || !newName || oldName === newName) return;
      const coordinator = selectedCoordinatorRef.current;
      const accepted = coordinator.accepted;
      const acceptedMatches = accepted?.id === stableId && accepted.name === oldName;
      const desiredMatches = coordinator.desiredName === oldName;
      if (!acceptedMatches && !desiredMatches) return;

      coordinator.retired.add(oldName);
      advanceSelectedRead(coordinator, oldName);

      const oldDebt = coordinator.reconciliationDebt.get(oldName);
      coordinator.reconciliationDebt.delete(oldName);
      if (oldDebt) {
        coordinator.reconciliationDebt.set(newName, { ...oldDebt, scheduled: false });
      }

      const oldBatch = coordinator.mutations.get(oldName);
      if (oldBatch) {
        coordinator.mutations.delete(oldName);
        oldBatch.identity = newName;
        coordinator.mutations.set(newName, oldBatch);
      }

      coordinator.identityEpoch += 1;
      coordinator.intentGeneration += 1;
      if (desiredMatches) coordinator.desiredName = newName;
      if (acceptedMatches && accepted) {
        commitSelected({ ...accepted, name: newName, display_name: newName });
      }
      advanceSelectedRead(coordinator, newName);
    },
    [commitSelected],
  );

  const launchSelectedRead = useCallback(
    async (options: {
      identity: string;
      identityEpoch: number;
      causalFloor: number;
      readGeneration: number;
      intentGeneration?: number;
      expectedCodes?: readonly string[];
      debtVersion?: number;
      refreshDefinitions?: boolean;
      rollbackOnFailure?: boolean;
      clearSelectionError?: boolean;
    }): Promise<SelectedReadOutcome> => {
      const coordinator = selectedCoordinatorRef.current;
      const isCurrent = () =>
        selectedMountedRef.current &&
        selectedReadIsCurrent(coordinator, options.identity, options.readGeneration) &&
        coordinator.identityEpoch === options.identityEpoch &&
        coordinator.desiredName === options.identity &&
        !coordinator.retired.has(options.identity);
      const finishDebt = (published: boolean, error?: unknown) => {
        if (options.debtVersion === undefined) return;
        const debt = coordinator.reconciliationDebt.get(options.identity);
        if (!debt || debt.version !== options.debtVersion) return;
        if (!published) {
          debt.scheduled = false;
          const waiters = debt.waiters.splice(0);
          for (const waiter of waiters) {
            waiter.resolve({ ok: false, error });
          }
          return;
        }
        coordinator.reconciliationDebt.delete(options.identity);
        const waiters = debt.waiters.splice(0);
        for (const waiter of waiters) {
          waiter.resolve({ ok: true });
        }
      };
      const continuationFor = () => {
        if (options.rollbackOnFailure === false) return undefined;
        const rollback = rollbackSelectedIntent();
        return rollback.identity && rollback.identity !== options.identity
          ? { nextIdentity: rollback.identity, intentGeneration: rollback.intentGeneration }
          : undefined;
      };
      // All selected-read terminal states pass through this one owner. The
      // executor only obtains a server result; retirement, debt completion,
      // rollback and error publication are decided here for both transport
      // shapes (HTTP ok:false and rejected requests).
      const finalizeSelectedRead = (value: unknown): SelectedReadOutcome => {
        if (!isCurrent()) return { kind: 'stale' };
        const result = value as {
          ok?: boolean;
          agent?: VibeAgentFull;
        } | null;
        if (result?.ok && result.agent?.name === options.identity) {
          if (options.clearSelectionError !== false) clearResourceError('selection');
          commitSelected(result.agent);
          finishDebt(true);
          return { kind: 'published' };
        }
        const code = errorCodeOf(value);
        if ((options.expectedCodes ?? SELECTED_DISAPPEARANCE_CODES).includes(code ?? '')) {
          const retirement = retireSelectedIdentityRef.current?.(options.identity, {
            refreshDefinitions: options.refreshDefinitions === true,
            cause: value,
          });
          return {
            kind: 'expected-retired',
            continuation: retirement?.resumedIdentity
              ? {
                  nextIdentity: retirement.resumedIdentity,
                  intentGeneration: retirement.intentGeneration,
                }
              : undefined,
          };
        }
        const continuation = continuationFor();
        setResourceError('selection', errorMessage(value) || tRef.current('errorBoundary.title'));
        finishDebt(false, value);
        return { kind: 'failed', error: value, continuation };
      };
      try {
        const params = options.expectedCodes ? { cache: false, expectedCodes: options.expectedCodes } : { cache: false };
        return finalizeSelectedRead(await api.getVibeAgent(options.identity, params));
      } catch (err) {
        return finalizeSelectedRead(err);
      }
    },
    [api, clearResourceError, commitSelected, rollbackSelectedIntent, setResourceError],
  );

  const scheduleSelectedRead = useCallback(
    (
      identity: string,
      options: {
        expectedCodes?: readonly string[];
        debtOnly?: boolean;
        causalFloor?: number;
        refreshDefinitions?: boolean;
        rollbackOnFailure?: boolean;
        clearSelectionError?: boolean;
        purpose?: SelectedReadPurpose;
        obligationId?: number;
      } = {},
    ): Promise<SelectedReadOutcome> | null => {
      if (!selectedMountedRef.current || !identity) return null;
      const coordinator = selectedCoordinatorRef.current;
      if (coordinator.retired.has(identity) || coordinator.desiredName !== identity) return null;
      const purpose = options.purpose ?? 'selection';
      const debt = coordinator.reconciliationDebt.get(identity);
      if (options.debtOnly && !debt) return null;
      if (coordinator.mutations.has(identity) && options.debtOnly) return null;
      // A selected-detail stage is the physical producer. Its purpose and
      // obligation are consumer metadata; the scheduler derives the causal
      // floor from the current identity debt so a same-agent selection can
      // join a live debt read instead of creating a debt-blind replacement.
      const debtVersion = debt?.version;
      const identityEpoch = coordinator.identityEpoch;
      const causalFloor = options.causalFloor ?? 0;
      const currentReadGeneration = coordinator.readGenerations.get(identity) ?? 0;
      const obligationId = options.obligationId ?? ++coordinator.nextObligationId;
      const intentGeneration = coordinator.intentGeneration;
      const existing = [...coordinator.stages.values()].find(
        (stage) =>
          !stage.invalidated &&
          !stage.settled &&
          stage.identity === identity &&
          stage.identityEpoch === identityEpoch &&
          stage.readGeneration === currentReadGeneration &&
          stage.causalFloor >= causalFloor &&
          (stage.debtVersion ?? 0) >= (debtVersion ?? 0),
      );
      if (existing) {
        if (debtVersion !== undefined) debt!.scheduled = true;
        return existing.promise;
      }

      // A new producer is the only place that advances an identity's read
      // generation. This invalidates every older stage, including a lower-floor
      // read that started before a newer mutation created reconciliation debt.
      const nextReadGeneration = advanceSelectedRead(coordinator, identity);
      if (debtVersion !== undefined) debt!.scheduled = true;
      const key = [identity, identityEpoch, nextReadGeneration, causalFloor, debtVersion ?? '-', purpose, obligationId].join('::');
      let resolveStage!: (outcome: SelectedReadOutcome) => void;
      const promise = new Promise<SelectedReadOutcome>((resolve) => {
        resolveStage = resolve;
      });
      const stage: SelectedReadStage = {
        key,
        identity,
        identityEpoch,
        causalFloor,
        intentGeneration,
        readGeneration: nextReadGeneration,
        debtVersion,
        obligationId,
        purpose,
        expectedCodes: options.expectedCodes,
        debtOnly: Boolean(options.debtOnly),
        refreshDefinitions: Boolean(options.refreshDefinitions),
        rollbackOnFailure: options.rollbackOnFailure !== false,
        clearSelectionError: options.clearSelectionError !== false,
        invalidated: false,
        promise,
        resolve: resolveStage,
        settled: false,
      };
      coordinator.stages.set(key, stage);
      coordinator.stageQueue.push(stage);
      drainSelectedStagesRef.current?.();
      return promise;
    },
    [],
  );
  scheduleSelectedReadRef.current = scheduleSelectedRead;

  const drainSelectedStages = useCallback(() => {
    const coordinator = selectedCoordinatorRef.current;
    if (coordinator.stageDrainActive) return;
    coordinator.stageDrainActive = true;
    while (selectedMountedRef.current && coordinator.stageQueue.length > 0) {
      const stage = coordinator.stageQueue.shift()!;
      if (stage.invalidated || !selectedMountedRef.current) {
        stage.settled = true;
        stage.resolve({ kind: 'stale' });
        if (coordinator.stages.get(stage.key) === stage) coordinator.stages.delete(stage.key);
        continue;
      }
      void (async () => {
        const outcome = await launchSelectedRead({
          identity: stage.identity,
          identityEpoch: stage.identityEpoch,
          causalFloor: stage.causalFloor,
          readGeneration: stage.readGeneration,
          intentGeneration: stage.intentGeneration,
          expectedCodes: stage.expectedCodes,
          debtVersion: stage.debtVersion,
          refreshDefinitions: stage.refreshDefinitions,
          rollbackOnFailure: stage.rollbackOnFailure,
          clearSelectionError: stage.clearSelectionError,
        });
        stage.settled = true;
        stage.resolve(outcome);
        if (coordinator.stages.get(stage.key) === stage) coordinator.stages.delete(stage.key);
        const continuation = outcome.continuation;
        if (
          !continuation ||
          !selectedMountedRef.current ||
          coordinator.intentGeneration !== continuation.intentGeneration ||
          coordinator.desiredName !== continuation.nextIdentity
        ) {
          drainSelectedStagesRef.current?.();
          return;
        }
        const attempted = coordinator.obligationAttempts.get(stage.obligationId) ?? new Set<string>();
        const nextDebt = coordinator.reconciliationDebt.get(continuation.nextIdentity);
        // A failed/retired user selection rolls back the visible intent but
        // does not start a passive read immediately. Reconnect or an existing
        // mutation debt is the external edge that may reconcile the fallback.
        if (stage.purpose === 'selection' && !nextDebt) {
          drainSelectedStagesRef.current?.();
          return;
        }
        if (!attempted.has(continuation.nextIdentity)) {
          attempted.add(continuation.nextIdentity);
          coordinator.obligationAttempts.set(stage.obligationId, attempted);
          scheduleSelectedReadRef.current?.(continuation.nextIdentity, {
            expectedCodes: stage.expectedCodes ?? SELECTED_DISAPPEARANCE_CODES,
            debtOnly: Boolean(nextDebt),
            refreshDefinitions: false,
            rollbackOnFailure: false,
            clearSelectionError: false,
            purpose: 'continuation',
            obligationId: stage.obligationId,
            causalFloor: stage.causalFloor,
          });
        }
        drainSelectedStagesRef.current?.();
      })();
    }
    coordinator.stageDrainActive = false;
  }, [launchSelectedRead]);
  drainSelectedStagesRef.current = () => {
    void drainSelectedStages();
  };

  const beginSelectedMutation = useCallback((identity: string) => {
    const coordinator = selectedCoordinatorRef.current;
    let batch = coordinator.mutations.get(identity);
    if (!batch) {
      batch = {
        id: ++coordinator.nextBatchId,
        identity,
        pending: 0,
        version: ++coordinator.nextMutationVersion,
        operations: [],
      };
      coordinator.mutations.set(identity, batch);
    }
    beginResourceError('mutation');
    batch.pending += 1;
    batch.version = ++coordinator.nextMutationVersion;
    // Keep the error watermark monotonic across batches. A delayed drain from
    // older work must never republish after this operation has begun.
    mutationErrorSequenceRef.current = Math.max(mutationErrorSequenceRef.current, batch.version);
    // A pre-mutation Definitions snapshot cannot publish after the detail
    // barrier. The settled transaction starts the next list publication.
    definitionsVersionRef.current.invalidate();
    const debt = coordinator.reconciliationDebt.get(identity);
    if (debt) debt.scheduled = false;
    advanceSelectedRead(coordinator, identity);
      const operation = { id: ++coordinator.nextOperationId, batchId: batch.id, identity, sequence: batch.version };
    batch.operations.push(operation);
    return operation;
  }, [beginResourceError]);

  const settleSelectedMutation = useCallback(
    (operation: SelectedMutationOperation, failure?: unknown): Promise<SelectedMutationResult> => {
      if (!selectedMountedRef.current) return Promise.resolve({ ok: false, error: new Error('Agent page unmounted') });
      const coordinator = selectedCoordinatorRef.current;
      const batch = [...coordinator.mutations.values()].find((candidate) => candidate.id === operation.batchId);
      if (!batch) return Promise.resolve({ ok: false, error: new Error('Agent mutation is no longer current') });
      if (failure) operation.failure = failure;
      batch.pending = Math.max(0, batch.pending - 1);
      if (batch.pending !== 0) {
        return new Promise<SelectedMutationResult>((resolve) => {
          operation.resolve = resolve;
        });
      }

      const identity = batch.identity;
      const version = batch.version;
      coordinator.mutations.delete(identity);
      const operations = [...batch.operations];
      for (const candidate of operations) {
        if (candidate.failure !== undefined) {
          // The batch is one publication edge, so expose a PATCH failure at
          // the batch watermark even when that operation started before a
          // sibling. Caller completion still uses the operation's own error.
          publishMutationError(candidate.failure, batch.version);
        }
      }
      const priorDebt = coordinator.reconciliationDebt.get(identity);
      if (!coordinator.retired.has(identity)) {
        coordinator.reconciliationDebt.set(identity, {
          version,
          scheduled: false,
          waiters: priorDebt?.waiters ?? [],
        });
      }
      const currentDebt = coordinator.reconciliationDebt.get(identity);
      const shouldDrain = Boolean(
        currentDebt &&
          coordinator.desiredName === identity &&
          !coordinator.retired.has(identity),
      );
      for (const candidate of operations) {
        candidate.joinedDetailBarrier = shouldDrain && candidate.failure === undefined;
      }
      const transaction = (async (): Promise<SelectedMutationResult> => {
        let detailResult: SelectedMutationResult = { ok: true };
        if (shouldDrain && currentDebt) {
          const completion = new Promise<SelectedMutationResult>((resolve) => {
            currentDebt.waiters.push({ resolve });
          });
          const drain = scheduleSelectedReadRef.current?.(identity, {
            debtOnly: true,
            expectedCodes: SELECTED_DISAPPEARANCE_CODES,
            rollbackOnFailure: false,
            purpose: 'debt',
            obligationId: version,
          });
          if (!drain && !currentDebt.scheduled) {
            const waiters = currentDebt.waiters.splice(0);
            for (const waiter of waiters) waiter.resolve({ ok: true });
          }
          detailResult = await completion;
        }
        // A brief list captured before the authoritative detail drain must not
        // publish after that detail. The barrier joins any newer list request.
        await refreshRef.current();
        return detailResult;
      })();
      const resultFor = (candidate: SelectedMutationOperation): Promise<SelectedMutationResult> => transaction.then((detailResult) => {
        if (candidate.failure !== undefined) {
          return { ok: false, error: candidate.failure };
        }
        if (candidate.joinedDetailBarrier && !detailResult.ok) {
          publishMutationError(detailResult.error, candidate.sequence);
          return detailResult;
        }
        return { ok: true };
      });
      for (const candidate of operations) {
        if (candidate.resolve) void resultFor(candidate).then(candidate.resolve);
      }
      return resultFor(operation);
    },
    [publishMutationError],
  );

  const reconcileSelected = useCallback(async (edgeEpoch?: number, causalFloor = 0) => {
    const coordinator = selectedCoordinatorRef.current;
    const identity = coordinator.desiredName;
    if (!identity || coordinator.mutations.has(identity)) return;
    if (edgeEpoch !== undefined && catchUpEpochRef.current !== edgeEpoch) return;
    if (
      edgeEpoch !== undefined &&
      [...coordinator.stages.values()].some(
        (stage) =>
          !stage.invalidated &&
          !stage.settled &&
          stage.identity === identity &&
          stage.obligationId === edgeEpoch &&
          stage.causalFloor >= causalFloor,
      )
    ) return;
    const debt = coordinator.reconciliationDebt.get(identity);
    scheduleSelectedReadRef.current?.(identity, {
      expectedCodes: SELECTED_DISAPPEARANCE_CODES,
      refreshDefinitions: true,
      clearSelectionError: true,
      debtOnly: Boolean(debt),
      purpose: 'reconcile',
      obligationId: edgeEpoch ?? ++coordinator.nextObligationId,
      causalFloor,
    });
  }, []);

  const reconcileGap = useCallback(async () => {
    const edgeEpoch = ++catchUpEpochRef.current;
    const applied = await refreshRef.current();
    if (!applied || !selectedMountedRef.current || catchUpEpochRef.current !== edgeEpoch) return;
    await reconcileSelected(edgeEpoch, edgeEpoch);
  }, [reconcileSelected]);

  useEffect(() => {
    selectedMountedRef.current = true;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void refreshOnboarding();
  }, [refreshOnboarding]);

  // Auto-select the default agent on first load so the detail panel has
  // something to show — eliminates the empty "select an agent" state
  // that confused users on first visit.
  useEffect(() => {
    const coordinator = selectedCoordinatorRef.current;
    if (
      !coordinator.autoSelectReason ||
      coordinator.autoSelectDismissed ||
      selected ||
      coordinator.accepted ||
      coordinator.desiredName ||
      agents.length === 0
    ) return;
    const available = agents.filter((agent) => !coordinator.retired.has(agent.name));
    const target = (defaultName && available.find((a) => a.name === defaultName)) || available[0];
    if (target) void selectAgent(target.name, false, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultName, agents]);

  // If the selection clears (agent deleted, or a refresh dropped it) drop the
  // mobile drill state too — otherwise the list stays max-lg:hidden with no
  // detail rendered, leaving the page blank with no way back.
  useEffect(() => {
    if (!selected) setDetailOpen(false);
  }, [selected]);

  const fetchRunningActiveCount = useCallback(async () => {
    if (!capabilities.can_use_agents) {
      setRunningActiveCount(null);
      return;
    }
    try {
      const result = await api.getRunningAgents();
      if (result.ok && result.counts) {
        // Badge = the true live-session count (active + idle + orphan) from the
        // running-agents snapshot. Deliberately sourced here (not from the graph
        // tab's counts) so it stays independent of the graph's project/time/
        // visibility filters — a narrowed graph must not shrink the badge.
        setRunningActiveCount(result.counts.total ?? 0);
      } else {
        setRunningActiveCount(null);
      }
    } catch {
      setRunningActiveCount(null);
    }
  }, [api, capabilities.can_use_agents]);

  // Keep the badge fresh on every tab (including 运行) so it never depends on
  // the graph view's filters.
  useEffect(() => {
    void fetchRunningActiveCount();
  }, [fetchRunningActiveCount]);

  useEffect(() => {
    if (!capabilities.can_use_agents) {
      setEventBridgeConnected(false);
      return;
    }
    return api.connectWorkbenchEvents({
      // Every gap ends here, whichever leg it was on, so this is the catch-up.
      // The bridge report is only the indicator's level: it comes with its own
      // `onConnected`, and refetching from both would pay twice for one gap.
      onConnected: () => {
        void reconcileGap();
        void refreshOnboarding();
        void fetchRunningActiveCount();
      },
      onEventBridgeStatus: ({ connected }) => setEventBridgeConnected(connected),
      onError: () => setEventBridgeConnected(false),
      onRunsUpdated: () => fetchRunningActiveCount(),
      onTurnStart: () => fetchRunningActiveCount(),
      onTurnEnd: () => fetchRunningActiveCount(),
      onSessionStatus: () => fetchRunningActiveCount(),
      onAuthorizationChanged: () => {
        void reconcileGap();
      },
    });
  }, [api, capabilities.can_use_agents, fetchRunningActiveCount, reconcileGap, refreshOnboarding]);

  useEffect(() => {
    if (!capabilities.can_use_agents) return;
    // Reconcile the badge even while SSE is connected: process death / orphan /
    // reap is a sampled snapshot with no run/session SSE event, so a slow
    // liveness poll keeps the count fresh (30s connected, 8s disconnected),
    // mirroring the old running list.
    const intervalMs = eventBridgeConnected ? 30000 : 8000;
    let timer: number | undefined;
    let cancelled = false;
    let inFlight = false;
    let pendingWake = false;

    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(tick, intervalMs);
        return;
      }
      if (inFlight) {
        pendingWake = true;
        return;
      }
      inFlight = true;
      window.clearTimeout(timer);
      try {
        await fetchRunningActiveCount();
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (pendingWake) {
        pendingWake = false;
        void tick();
        return;
      }
      timer = window.setTimeout(tick, intervalMs);
    };

    timer = window.setTimeout(tick, intervalMs);
    const stopReactivation = onPageReactivated(() => void tick());
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      stopReactivation();
    };
  }, [capabilities.can_use_agents, eventBridgeConnected, fetchRunningActiveCount]);

  const selectAgent = useCallback(
    async (name: string, openDetail = false, auto = false) => {
      beginSelectedIntent(name, { auto, openDetail, source: auto ? 'auto' : 'user' });
      clearResourceError('selection');
      await scheduleSelectedReadRef.current?.(name, {
        expectedCodes: SELECTED_DISAPPEARANCE_CODES,
        refreshDefinitions: true,
        purpose: auto ? 'selection' : 'selection',
        obligationId: selectedCoordinatorRef.current.intentGeneration,
      });
    },
    [beginSelectedIntent, clearResourceError],
  );

  const dismissSelected = useCallback(() => {
    const coordinator = selectedCoordinatorRef.current;
    beginSelectedIntent(null);
    coordinator.autoSelectDismissed = true;
    coordinator.autoSelectReason = null;
    commitSelected(null);
    setDetailOpen(false);
  }, [beginSelectedIntent, commitSelected]);

  // Apply text search + backend filter; backend grouping is a layout
  // concern that operates on the filtered set.
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return agents.filter((agent) => {
      if (backendFilter !== 'all' && agent.backend !== backendFilter) return false;
      if (!q) return true;
      return (
        agent.name.toLowerCase().includes(q) ||
        (agent.description ?? '').toLowerCase().includes(q) ||
        (agent.model ?? '').toLowerCase().includes(q)
      );
    });
  }, [agents, search, backendFilter]);

  const grouped = useMemo(() => {
    const groups: Record<Backend, VibeAgentBrief[]> = { claude: [], opencode: [], codex: [] };
    for (const agent of filtered) {
      const key = (agent.backend as Backend) in groups ? (agent.backend as Backend) : null;
      if (key) groups[key].push(agent);
    }
    return groups;
  }, [filtered]);

  const onCreated = (agent: VibeAgentFull) => {
    beginSelectedIntent(agent.name);
    commitSelected(agent);
    void refresh();
    void refreshOnboarding();
  };


  const updateField = async (patch: VibeAgentUpdatePayload): Promise<void> => {
    if (!selected) return;
    const name = selected.name;
    const operation = beginSelectedMutation(name);
    let mutationSettled = false;
    try {
      const result = await api.updateVibeAgent(name, patch);
      const settled = await settleSelectedMutation(operation, result.ok ? undefined : result);
      mutationSettled = true;
      if (!settled.ok) throw settled.error;
    } catch (err) {
      if (!mutationSettled) {
        const settled = await settleSelectedMutation(operation, err);
        mutationSettled = true;
        if (!settled.ok) throw settled.error;
      }
      throw err;
    }
  };

  // Promote the selected agent to the global default so plain "new chat"
  // / IM routing without an explicit agent lands here. Throws on failure
  // so the detail panel can surface a toast.
  const onSetDefault = async () => {
    if (!selected) return;
    await api.setDefaultVibeAgent(selected.name);
    setDefaultName(selected.name);
    refresh();
  };

  // Rename is a selected mutation too: migrate the stable entity before the
  // batch settles so its authoritative drain routes through the new name.
  const onRename = async (newName: string) => {
    if (!selected) return;
    const oldName = selected.name;
    const operation = beginSelectedMutation(oldName);
    let settled = false;
    try {
      const result = await api.updateVibeAgent(oldName, { name: newName });
      if (!result.ok) {
        await settleSelectedMutation(operation, result);
        settled = true;
        throw result;
      }
      migrateSelectedIdentity(oldName, newName, selected.id);
      const mutationResult = await settleSelectedMutation(operation);
      settled = true;
      if (!mutationResult.ok) throw mutationResult.error;
      void refreshOnboarding();
    } catch (err) {
      if (!settled) await settleSelectedMutation(operation, err);
      throw err;
    }
  };

  const onDelete = async () => {
    if (!selected || isSystemAgent(selected)) return;
    const confirmed = window.confirm(t('agents.deleteConfirm', { name: selected.name }));
    if (!confirmed) return;
    const name = selected.name;
    const errorGeneration = beginResourceError('mutation');
    try {
      const result = await api.removeVibeAgent(name);
      if (result.ok) {
        retireSelectedIdentityRef.current?.(name, { refreshDefinitions: true });
        void refreshOnboarding();
      } else {
        setResourceError('mutation', errorMessage(result) || t('errorBoundary.title'), errorGeneration);
      }
    } catch (err) {
      setResourceError('mutation', errorMessage(err) || t('errorBoundary.title'), errorGeneration);
    }
  };

  const onImport = async (from: Backend) => {
    setImporting(from);
    try {
      const result = await api.importVibeAgents({ from, all: true });
      if (result.ok) {
        // Backend returns newly imported agents under `imported` (see
        // vibe/api.py::import_vibe_agents); `created` was always undefined so
        // the toast reported 0 even on a successful import.
        const imported = result.imported?.length ?? 0;
        const skipped = result.skipped?.length ?? 0;
        if (imported === 0 && skipped === 0) {
          // Nothing on disk for this backend — say where we looked instead of a
          // confusing "imported 0" success toast.
          showToast(t('agents.importNoneFound', { backend: BACKEND_LABEL[from] }), 'warning');
        } else {
          showToast(t('agents.importSuccess', { imported, skipped }), 'success');
        }
        refresh();
        void refreshOnboarding();
      } else {
        showToast(
          t('agents.importFailed', { error: result.message || result.error || result.code || 'unknown' }),
          'error',
        );
      }
    } catch (err) {
      showToast(t('agents.importFailed', { error: errorMessage(err) ?? String(err) }), 'error');
    } finally {
      setImporting(null);
    }
  };

  const onOnboardAgents = async () => {
    if (onboardingSubmitting) return;
    setOnboardingSubmitting(true);
    onboardingVersionRef.current.invalidate();
    try {
      const result = await api.onboardVibeAgents();
      showToast(
        result.sync?.ok === false
          ? t('agents.onboarding.savedPending')
          : t('agents.onboarding.saved', { count: result.created ?? 0 }),
        result.sync?.ok === false ? 'warning' : 'success',
      );
    } catch (err) {
      showToast(t('agents.onboarding.failed', { error: errorMessage(err) ?? String(err) }), 'error');
    } finally {
      let settlement = refreshOnboarding();
      await settlement;
      while (selectedMountedRef.current) {
        const latest = onboardingSettlementRef.current;
        if (!latest || latest === settlement) break;
        settlement = latest;
        await settlement;
      }
      setOnboardingSubmitting(false);
    }
  };

  const totalShown = filtered.length;
  const noMatches = totalShown === 0 && agents.length > 0;

  return (
    <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-5 py-2">
      <CapabilityTabs />
      {/* Header — shared WorkbenchPageHeader (design.pen: 40px mint icon + title + subtitle). */}
      <WorkbenchPageHeader
        icon={<Bot className="size-5" />}
        title={t('agents.title')}
        subtitle={t('agents.subtitle', { count: agents.length })}
        actions={
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={() => {
              void refresh();
              void refreshOnboarding();
            }}
          >
            <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
            {t('common.refresh')}
          </Button>
        }
      />

      {/* Local runtime diagnostics are intentionally unavailable remotely. */}
      <div className="flex items-center gap-0 overflow-x-auto border-b border-border">
        {visibleTabs.map((key) => {
          const active = activeTab === key;
          return (
            <button
              key={key}
              type="button"
              onClick={() => selectAgentsTab(key)}
              className={clsx(
                'flex shrink-0 items-center gap-2 whitespace-nowrap px-4 py-3 text-[13px] transition',
                active
                  ? 'border-b-2 border-violet font-bold text-violet-ink'
                  : 'font-medium text-muted hover:text-foreground',
              )}
            >
              {key === 'definitions' ? (
                <Layers className={clsx('size-3.5', active ? 'text-violet-ink' : 'text-muted')} />
              ) : (
                <Activity className={clsx('size-3.5', active ? 'text-violet-ink' : 'text-muted')} />
              )}
              {t(`agents.tabs.${key}`)}
              {key === 'running' && (
                <span
                  className={clsx(
                    'rounded-full border px-1.5 py-0 font-mono text-[9px] font-bold',
                    active
                      ? 'border-violet/30 bg-violet/[0.10] text-violet-ink'
                      : 'border-border-strong bg-foreground/[0.04] text-muted',
                  )}
                >
                  {runningActiveCount === null ? '—' : runningActiveCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* The run graph follows the Agent capability. */}
      {capabilities.can_use_agents && activeTab === 'running' && <AgentGraphTab />}

      {activeTab === 'definitions' && canOnboardAgents && onboardingInventory && (
        <OrganizationAgentOnboarding
          inventory={onboardingInventory}
          expanded={onboardingExpanded}
          submitting={onboardingSubmitting}
          className={detailOpen ? 'max-lg:hidden' : undefined}
          onExpandedChange={setOnboardingExpanded}
          onOnboard={onOnboardAgents}
        />
      )}

      {/* Toolbar — design.pen Imduv: search + backend filter + spacer + Import + 新建 Agent */}
      <div className={clsx('flex flex-wrap items-center gap-2.5', activeTab === 'running' ? 'hidden' : detailOpen && 'max-lg:hidden')}>
        <div className="flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background px-3 transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring sm:w-[320px]">
          <Search className="size-3.5 shrink-0 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('agents.searchPlaceholder')}
            className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
          />
        </div>
        <BackendFilter value={backendFilter} onChange={setBackendFilter} />
        <div className="flex-1" />
        {canEditAgents ? (
          <>
            <Button type="button" variant="outline" size="xs" onClick={() => setShowGlobalPrompts(true)}>
              <FileText className="size-3.5" />
              {t('globalPrompts.button')}
            </Button>
            <ImportMenu onImport={onImport} importing={importing} />
            <Button type="button" variant="brand" size="xs" onClick={() => setShowNew(true)}>
              <Plus />
              {t('agents.newAgent')}
            </Button>
          </>
        ) : (
          <Badge variant="secondary" title={t('agents.remoteReadOnlyHint')}>
            <Lock className="size-3" />
            {t('agents.remoteReadOnly')}
          </Badge>
        )}
      </div>

      {activeTab === 'definitions' && error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
          {error}
        </div>
      )}

      {/* Body — list + detail. The detail column only renders when a row
          is selected; the empty "select an agent" placeholder used to
          dominate the right side of a fresh page. With auto-select on
          mount it's rarely needed; when it is empty we just collapse
          back to a single column.
          Hidden when Running tab is active; all hooks stay mounted. */}
      <div
        className={clsx(
          'grid gap-5',
          activeTab === 'running' && 'hidden',
          // `minmax(0,1fr)` + `min-w-0` keep the list column shrinkable; bare
          // `1fr` would let a long agent row push the fixed detail card off-screen.
          selected ? 'grid-cols-1 lg:grid-cols-[minmax(0,1fr)_420px]' : 'grid-cols-1',
        )}
      >
        <div className={clsx('flex min-w-0 flex-col gap-4', detailOpen && 'max-lg:hidden')}>
          {BACKEND_ORDER.map((backend) => {
            const items = grouped[backend];
            if (!items || items.length === 0) return null;
            return (
              <div key={backend} className="flex flex-col gap-2">
                <div className="flex items-center gap-2 px-1">
                  <Bot className={clsx('size-3.5', BACKEND_ICON_CLASS[backend])} />
                  <span className={clsx('text-[13px] font-bold', BACKEND_ICON_CLASS[backend])}>
                    {BACKEND_LABEL[backend]}
                  </span>
                  <span className="font-mono text-[10px] text-muted">
                    {items.length} agents
                  </span>
                </div>
                <div className="flex flex-col gap-2">
                  {items.map((agent) => (
                    <AgentRow
                      key={agent.id}
                      agent={agent}
                      isSelected={selected?.name === agent.name}
                      isDefault={defaultName === agent.name}
                      onSelect={() => selectAgent(agent.name, true)}
                    />
                  ))}
                </div>
              </div>
            );
          })}

          {agents.length === 0 && !loading && (
            <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-16 text-center">
              <Bot className="size-8 text-muted" />
              <div className="text-[14px] font-semibold text-foreground">{t('agents.empty')}</div>
              {canEditAgents && (
                <Button type="button" variant="brand" size="sm" onClick={() => setShowNew(true)}>
                  <Plus />
                  {t('agents.newAgent')}
                </Button>
              )}
            </div>
          )}

          {noMatches && (
            <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-10 text-center text-[12px] text-muted">
              {t('agents.noSearchMatch')}
            </div>
          )}
        </div>

        {selected && (
          <div className={clsx('self-start rounded-2xl border border-border-strong bg-surface p-5', !detailOpen && 'max-lg:hidden')}>
            <AgentDetailPanel
              key={selected.id}
              agent={selected}
              isDefault={defaultName === selected.name}
              canEdit={canEditAgents}
              onChange={updateField}
              onSetDefault={onSetDefault}
              onRename={onRename}
              onDelete={onDelete}
              onClose={dismissSelected}
            />
          </div>
        )}
      </div>

      {canEditAgents && (
        <>
          <NewAgentDialog open={showNew} onClose={() => setShowNew(false)} onCreated={onCreated} />
          <GlobalPromptsDialog open={showGlobalPrompts} onClose={() => setShowGlobalPrompts(false)} />
        </>
      )}
    </div>
  );
};

interface OrganizationAgentOnboardingProps {
  inventory: VibeAgentOnboardingResult;
  expanded: boolean;
  submitting: boolean;
  className?: string;
  onExpandedChange: (expanded: boolean) => void;
  onOnboard: () => void;
}

const OrganizationAgentOnboarding: React.FC<OrganizationAgentOnboardingProps> = ({
  inventory,
  expanded,
  submitting,
  className,
  onExpandedChange,
  onOnboard,
}) => {
  const { t } = useTranslation();
  const counts = inventory.counts;
  const onboarded = counts.private + counts.published;

  return (
    <section className={clsx('border-y border-border bg-surface-2/60 py-4', className)}>
      <div className="flex flex-col gap-4 px-1 sm:px-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="flex min-w-0 flex-1 items-start gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-md border border-mint/30 bg-mint-soft text-mint-ink">
              <ShieldCheck className="size-4" />
            </span>
            <div className="min-w-0">
              <div className="text-[13px] font-bold text-foreground">{t('agents.onboarding.title')}</div>
              <div className="mt-0.5 text-[11px] leading-5 text-muted">
                {t('agents.onboarding.summary', {
                  total: counts.total,
                  custom: counts.custom,
                  system: counts.system,
                })}
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={counts.not_onboarded > 0 ? 'warning' : 'secondary'}>
              {t('agents.onboarding.notOnboardedCount', { count: counts.not_onboarded })}
            </Badge>
            <Badge variant="secondary">{t('agents.onboarding.privateCount', { count: counts.private })}</Badge>
            <Badge variant="success">{t('agents.onboarding.publishedCount', { count: counts.published })}</Badge>
            {counts.conflicts > 0 && (
              <Badge variant="destructive">{t('agents.onboarding.conflictCount', { count: counts.conflicts })}</Badge>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => onExpandedChange(!expanded)}
            className="flex min-w-0 items-center gap-2 text-[12px] font-medium text-foreground hover:text-mint-ink"
          >
            <ChevronRight className={clsx('size-3.5 shrink-0 transition-transform', expanded && 'rotate-90')} />
            {t('agents.onboarding.inventory', { count: counts.total })}
          </button>
          <div className="flex-1" />
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={onOnboard}
            disabled={submitting || counts.not_onboarded === 0}
          >
            {submitting ? <Loader2 className="animate-spin" /> : <LockKeyhole />}
            {counts.not_onboarded > 0
              ? t('agents.onboarding.onboardPrivate')
              : t('agents.onboarding.onboarded')}
          </Button>
          {inventory.console_url && onboarded > 0 && (
            <Button asChild variant="accent" size="xs">
              <a href={inventory.console_url} target="_blank" rel="noreferrer">
                <ExternalLink />
                {t('agents.onboarding.manageAccess')}
              </a>
            </Button>
          )}
        </div>

        {expanded && (
          <div className="divide-y divide-border border-t border-border">
            {inventory.agents.map((agent) => {
              const system = isSystemAgent(agent);
              const statusVariant =
                agent.status === 'published'
                  ? 'success'
                  : agent.status === 'not_onboarded'
                    ? 'warning'
                    : agent.status === 'managed_elsewhere'
                      ? 'destructive'
                      : 'secondary';
              return (
                <div key={agent.id} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-3 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-[12px] font-semibold text-foreground">{agent.name}</div>
                    <div className="truncate font-mono text-[10px] text-muted">
                      {agent.backend} · {system ? t('agents.onboarding.system') : t('agents.onboarding.custom')}
                    </div>
                  </div>
                  <Badge variant={statusVariant} className="max-w-[45vw]">
                    <span className="truncate">{t(`agents.onboarding.status.${agent.status}`)}</span>
                  </Badge>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
};

// One row in the backend-grouped list. Hover state + click selects.
interface AgentRowProps {
  agent: VibeAgentBrief;
  isSelected: boolean;
  isDefault: boolean;
  onSelect: () => void;
}

const AgentRow: React.FC<AgentRowProps> = ({ agent, isSelected, isDefault, onSelect }) => {
  const { t } = useTranslation();
  const description = [agent.model, agent.reasoning_effort, agent.description].filter(Boolean).join(' · ');
  return (
    <button
      type="button"
      onClick={onSelect}
      className={clsx(
        'flex items-center gap-3 rounded-xl border px-4 py-3 text-left transition',
        isSelected
          ? 'border-mint/40 bg-mint-soft shadow-glow-sm-mint'
          : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
      )}
    >
      <div className="flex flex-1 flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="text-[14px] font-semibold text-foreground">{agent.name}</span>
          {isDefault && (
            <Badge variant="success" className="px-1.5 py-0 text-[9px] font-mono uppercase">
              {t('common.default')}
            </Badge>
          )}
          {isSystemAgent(agent) && (
            <Badge variant="secondary" className="px-1.5 py-0 text-[9px] font-mono uppercase">
              {t('common.systemSession')}
            </Badge>
          )}
        </div>
        {description && <div className="text-[11px] text-muted">{description}</div>}
      </div>
      <Badge variant={agent.enabled ? 'success' : 'secondary'} className="font-mono uppercase">
        <span className={clsx('size-1.5 rounded-full', agent.enabled ? 'bg-mint' : 'bg-muted')} />
        {agent.enabled ? t('agents.statusEnabled') : t('agents.statusDisabled')}
      </Badge>
    </button>
  );
};

interface BackendFilterProps {
  value: Backend | 'all';
  onChange: (next: Backend | 'all') => void;
}

// Compact Popover trigger that mirrors design.pen dMFRl — funnel icon +
// "Backend: All" label + chevron. Replaces the old hand-rolled checkbox.
const BackendFilter: React.FC<BackendFilterProps> = ({ value, onChange }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const label = value === 'all' ? t('agents.backendAll') : BACKEND_LABEL[value];
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1.5 rounded-md border border-border-strong bg-surface px-3 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
        >
          <Funnel className="size-3 text-muted" />
          <span className="text-muted">{t('agents.backendFilter')}:</span>
          <span>{label}</span>
          <ChevronDown className="size-3 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[180px] p-1">
        {(['all', ...BACKEND_ORDER] as const).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => {
              onChange(key);
              setOpen(false);
            }}
            className={clsx(
              'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition',
              value === key ? 'bg-cyan-soft text-cyan-ink' : 'text-foreground hover:bg-foreground/[0.04]',
            )}
          >
            {key !== 'all' && <Bot className={clsx('size-3.5', BACKEND_ICON_CLASS[key])} />}
            <span>{key === 'all' ? t('agents.backendAll') : BACKEND_LABEL[key]}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
};

interface ImportMenuProps {
  onImport: (from: Backend) => void;
  importing: Backend | null;
}

// Outline Button that opens a popover with one entry per backend. The
// backend supports bulk import via `from=<backend>&all=true`, which
// surfaces every installed agent definition the user already has on
// disk for that backend.
const ImportMenu: React.FC<ImportMenuProps> = ({ onImport, importing }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline" size="xs" disabled={importing !== null}>
          {importing ? <Loader2 className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />}
          {t('agents.import')}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[200px] p-1">
        {BACKEND_ORDER.map((backend) => (
          <button
            key={backend}
            type="button"
            disabled={importing !== null}
            onClick={() => {
              onImport(backend);
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04] disabled:opacity-50"
          >
            <Bot className={clsx('size-3.5', BACKEND_ICON_CLASS[backend])} />
            <span>{t(`agents.importFrom${BACKEND_LABEL[backend]}` as const)}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
};

interface DetailProps {
  agent: VibeAgentFull;
  isDefault: boolean;
  /** False on remote instances, where every mutating control is unavailable. */
  canEdit: boolean;
  onChange: (patch: VibeAgentUpdatePayload) => Promise<void>;
  onSetDefault: () => Promise<void>;
  onRename: (newName: string) => Promise<void>;
  onDelete: () => void;
  onClose: () => void;
}

// Mirrors design.pen s7QaWQ. Header (name + close X) → Enable card →
// Name → Backend (read-only) → Model (Combobox) → Reasoning effort →
// System Prompt (collapsible) → footer Run / Delete. Name is editable
// for user agents. The backend renames the row and its references atomically;
// system agents keep their locked identity. On a remote instance `canEdit` is
// false and the panel degrades to a read-only view of the same fields.
const AgentDetailPanel: React.FC<DetailProps> = ({ agent, isDefault, canEdit, onChange, onSetDefault, onRename, onDelete, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  // System agents are locked everywhere; remote access locks every agent.
  const locked = isSystemAgent(agent) || !canEdit;
  const system = isSystemAgent(agent);
  const [name, setName] = useState(agent.name);
  const [renaming, setRenaming] = useState(false);
  const [settingDefault, setSettingDefault] = useState(false);
  const [description, setDescription] = useState(agent.description ?? '');
  const [model, setModel] = useState(agent.model ?? '');
  // The panel shows what the server holds, so an unset effort stays unset. A
  // `?? 'medium'` here would light a segment nobody chose, and switching models
  // would then send only `model` while the record kept no effort at all.
  const [effort, setEffort] = useState<string | null>(agent.reasoning_effort ?? null);
  const [systemPrompt, setSystemPrompt] = useState(agent.system_prompt ?? '');
  const [systemPromptOpen, setSystemPromptOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorSeedRevision, setEditorSeedRevision] = useState(0);
  const editorDraftRef = useRef(agent.system_prompt ?? '');
  const editorDirtyRef = useRef(false);
  const editorClosingRef = useRef(false);
  const editorBaselineRef = useRef(agent.system_prompt ?? '');
  type EditableField = 'name' | 'description' | 'model' | 'effort' | 'systemPrompt';
  const fieldRevisionRef = useRef<Record<EditableField, number>>({
    name: 0,
    description: 0,
    model: 0,
    effort: 0,
    systemPrompt: 0,
  });
  const submittedRevisionRef = useRef<Record<EditableField, number>>({
    name: 0,
    description: 0,
    model: 0,
    effort: 0,
    systemPrompt: 0,
  });
  const pendingRevisionRef = useRef<Record<EditableField, number | null>>({
    name: null,
    description: null,
    model: null,
    effort: null,
    systemPrompt: null,
  });
  const [modelCatalogs, setModelCatalogs] = useState<
    Record<
      string,
      {
        modelOptions: ComboboxOption[];
        reasoningOptions: Record<string, { value: string; label: string }[]>;
      }
    >
  >({});
  const [running, setRunning] = useState(false);

  const serverSnapshotRef = useRef<{
    id: string;
    name: string;
    description: string;
    model: string;
    effort: string | null;
    systemPrompt: string;
  }>({
    id: agent.id,
    name: agent.name,
    description: agent.description ?? '',
    model: agent.model ?? '',
    effort: agent.reasoning_effort ?? null,
    systemPrompt: agent.system_prompt ?? '',
  });

  const activeModelCatalog = modelCatalogs[agent.backend];
  const modelOptions = activeModelCatalog?.modelOptions ?? [];
  const reasoningOptions = activeModelCatalog?.reasoningOptions ?? {};

  useEffect(() => {
    const previous = serverSnapshotRef.current;
    const next = {
      id: agent.id,
      name: agent.name,
      description: agent.description ?? '',
      model: agent.model ?? '',
      effort: agent.reasoning_effort ?? null,
      systemPrompt: agent.system_prompt ?? '',
    };
    const snapshotChanged =
      previous.id !== next.id ||
      previous.name !== next.name ||
      previous.description !== next.description ||
      previous.model !== next.model ||
      previous.effort !== next.effort ||
      previous.systemPrompt !== next.systemPrompt;
    if (!snapshotChanged) return;
    if (previous.id !== next.id) {
      fieldRevisionRef.current = { name: 0, description: 0, model: 0, effort: 0, systemPrompt: 0 };
      submittedRevisionRef.current = { name: 0, description: 0, model: 0, effort: 0, systemPrompt: 0 };
      pendingRevisionRef.current = { name: null, description: null, model: null, effort: null, systemPrompt: null };
      setName(next.name);
      setDescription(next.description);
      setModel(next.model);
      setEffort(next.effort);
      setSystemPrompt(next.systemPrompt);
      setSystemPromptOpen(false);
      setEditorOpen(false);
      editorClosingRef.current = false;
      editorDraftRef.current = next.systemPrompt;
      editorBaselineRef.current = next.systemPrompt;
      editorDirtyRef.current = false;
      setEditorSeedRevision((revision) => revision + 1);
    } else {
      // Same-identity reconciliation compares raw text drafts to the raw
      // accepted baseline. Submit-time trimming belongs only to the blur/save
      // boundary, so an active leading/trailing-space draft remains visible.
      const pending = pendingRevisionRef.current;
      if (pending.name === null && name === previous.name) setName(next.name);
      if (pending.description === null && description === previous.description) setDescription(next.description);
      if (pending.model === null && model === previous.model) setModel(next.model);
      if (pending.effort === null && effort === previous.effort) setEffort(next.effort);
      if (pending.systemPrompt === null && systemPrompt === previous.systemPrompt) setSystemPrompt(next.systemPrompt);
      if (!editorDirtyRef.current && editorOpen) {
        editorDraftRef.current = next.systemPrompt;
        editorBaselineRef.current = next.systemPrompt;
        editorDirtyRef.current = false;
        setEditorSeedRevision((revision) => revision + 1);
      }
    }
    serverSnapshotRef.current = next;
  }, [agent.id, agent.name, agent.description, agent.model, agent.reasoning_effort, agent.system_prompt, editorOpen, name, description, model, effort, systemPrompt]);

  // Load model catalog for the agent's backend so the Combobox can offer
  // suggestions. Keeps `allowCustomValue` so users can type a model the
  // backend doesn't know about yet (e.g. a freshly-released preview).
  useEffect(() => {
    return loadBackendModelsWithRefresh(
      api,
      agent.backend,
      ({ models, modelLabels, reasoningOptions: opts }) => {
        setModelCatalogs((current) => ({
          ...current,
          [agent.backend]: {
            modelOptions: models.map((m) => ({ value: m, label: modelOptionLabel(m, modelLabels) })),
            reasoningOptions: opts ?? {},
          },
        }));
      },
      () => {
        setModelCatalogs((current) => ({
          ...current,
          [agent.backend]: { modelOptions: [], reasoningOptions: {} },
        }));
      },
    );
  }, [agent.backend, api]);

  const lockHint = system
    ? t('agents.detail.systemLocked')
    : canEdit
      ? undefined
      : t('agents.remoteReadOnlyHint');
  const systemPromptTokens = estimateTokens(systemPrompt);
  // Effort options follow the backend + selected model when the catalog provides them.
  const effortOptions = resolveEffortOptions(agent.backend, model, reasoningOptions);
  const markFieldEdit = (field: keyof typeof fieldRevisionRef.current) => {
    fieldRevisionRef.current[field] += 1;
    return fieldRevisionRef.current[field];
  };
  const markFieldSubmitted = (field: keyof typeof submittedRevisionRef.current) => {
    submittedRevisionRef.current[field] = fieldRevisionRef.current[field];
  };
  const cancelFieldEdit = (field: EditableField, value: string) => {
    const revision = fieldRevisionRef.current[field];
    pendingRevisionRef.current[field] = null;
    submittedRevisionRef.current[field] = revision;
    if (fieldRevisionRef.current[field] === revision) {
      if (field === 'name') setName(value);
      if (field === 'description') setDescription(value);
      if (field === 'model') setModel(value);
      if (field === 'effort') setEffort(value);
      if (field === 'systemPrompt') setSystemPrompt(value);
    }
  };
  const submitFields = (patch: VibeAgentUpdatePayload, fields: EditableField[]): Promise<void> => {
    const revisions = fields.map((field) => {
      const revision = fieldRevisionRef.current[field];
      pendingRevisionRef.current[field] = revision;
      return { field, revision };
    });
    return onChange(patch).then(() => {
      for (const { field, revision } of revisions) {
        if (pendingRevisionRef.current[field] !== revision) continue;
        pendingRevisionRef.current[field] = null;
        submittedRevisionRef.current[field] = revision;
        if (fieldRevisionRef.current[field] !== revision) continue;
        const snapshot = serverSnapshotRef.current;
        if (field === 'name') setName(snapshot.name);
        if (field === 'description') setDescription(snapshot.description ?? '');
        if (field === 'model') setModel(snapshot.model ?? '');
        if (field === 'effort') setEffort(snapshot.effort);
        if (field === 'systemPrompt') {
          const value = snapshot.systemPrompt ?? '';
          setSystemPrompt(value);
          if (!editorDirtyRef.current && editorOpen) {
            editorDraftRef.current = value;
            editorBaselineRef.current = value;
            setEditorSeedRevision((seed) => seed + 1);
          }
        }
      }
    }).catch((err) => {
      // A PATCH or its authoritative drain failed. The caller must see the
      // rejection, while the latest server snapshot owns any field without a
      // newer local edit. Keep an open modal's private draft untouched.
      for (const { field, revision } of revisions) {
        if (pendingRevisionRef.current[field] !== revision) continue;
        pendingRevisionRef.current[field] = null;
        submittedRevisionRef.current[field] = revision;
        if (fieldRevisionRef.current[field] !== revision) continue;
        const snapshot = serverSnapshotRef.current;
        if (field === 'name') setName(snapshot.name);
        if (field === 'description') setDescription(snapshot.description ?? '');
        if (field === 'model') setModel(snapshot.model ?? '');
        if (field === 'effort') setEffort(snapshot.effort);
        if (field === 'systemPrompt') setSystemPrompt(snapshot.systemPrompt ?? '');
      }
      throw err;
    });
  };

  // Inline controls are background mutations: the coordinator records and
  // renders failures, while this single owner consumes the rejection so JSX
  // callbacks never create unhandled promises. The modal save intentionally
  // bypasses this helper so EditorDialog can keep a failed draft open.
  const consumeBackgroundMutation = (operation: Promise<unknown>) => {
    void operation.catch(() => undefined);
  };

  // Only user Agents can be renamed; the backend moves every durable name
  // reference in the same transaction.
  const commitRename = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === agent.name) {
      cancelFieldEdit('name', serverSnapshotRef.current.name);
      return;
    }
    if (locked) {
      cancelFieldEdit('name', serverSnapshotRef.current.name);
      return;
    }
    const revision = fieldRevisionRef.current.name;
    pendingRevisionRef.current.name = revision;
    submittedRevisionRef.current.name = revision;
    setRenaming(true);
    try {
      await onRename(trimmed);
      pendingRevisionRef.current.name = null;
      if (fieldRevisionRef.current.name === revision) setName(trimmed);
      showToast(t('agents.renameSuccess'), 'success');
    } catch (err) {
      showToast(errorMessage(err) ?? t('errorBoundary.title'), 'error');
      cancelFieldEdit('name', serverSnapshotRef.current.name);
    } finally {
      setRenaming(false);
    }
  };

  const handleSetDefault = async () => {
    if (settingDefault) return;
    setSettingDefault(true);
    try {
      await onSetDefault();
      showToast(t('agents.detail.defaultSet', { name: agent.name }), 'success');
    } catch (err) {
      showToast(errorMessage(err) ?? String(err), 'error');
    } finally {
      setSettingDefault(false);
    }
  };

  return (
    <div className="flex flex-col gap-3.5">
      {/* Header row — design.pen j5dGQ8 without DEFAULT badge (now read-
          only via the list-row pill; the panel always shows the agent's
          current identity, not its "is-default" status). */}
      <div className="flex items-start gap-2.5">
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="truncate text-[16px] font-bold text-foreground">{agent.name}</div>
          <div className="truncate text-[10px] text-muted">
            Vibe Agent · {agent.backend} backend
          </div>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onClose}
          aria-label={t('common.close')}
          className="size-6"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </Button>
      </div>

      {/* Enable toggle — design.pen EWTY7 */}
      <div
        className={clsx(
          'flex items-center justify-between gap-3 rounded-[10px] border px-3.5 py-3',
          agent.enabled ? 'border-mint/40 bg-mint-soft' : 'border-border-strong bg-surface-2',
        )}
      >
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-[13px] font-bold text-foreground">{t('agents.detail.enabled')}</span>
          <span className="text-[11px] text-muted">{t('agents.detail.enabledHint')}</span>
        </div>
        <Switch
          checked={agent.enabled}
          onCheckedChange={(next) => {
            consumeBackgroundMutation(onChange({ enabled: next }));
          }}
          disabled={!canEdit}
          title={canEdit ? undefined : t('agents.remoteReadOnlyHint')}
          label={t('agents.detail.enabled')}
        />
      </div>

      {/* Default routing — promotes this agent to the global default so a
          plain "new chat" (and IM routing without an explicit agent)
          lands here. Restores the set-default control dropped in the
          workbench rebuild; a disabled agent can't be the default. */}
      <div
        className={clsx(
          'flex items-center justify-between gap-3 rounded-[10px] border px-3.5 py-3',
          isDefault ? 'border-mint/40 bg-mint-soft' : 'border-border-strong bg-surface-2',
        )}
      >
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-[13px] font-bold text-foreground">{t('agents.detail.defaultTitle')}</span>
          <span className="text-[11px] text-muted">{t('agents.detail.defaultHint')}</span>
        </div>
        {isDefault ? (
          <Badge variant="success" className="font-mono uppercase">
            <Star className="size-3" />
            {t('agents.detail.defaultActive')}
          </Badge>
        ) : !canEdit ? null : (
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={handleSetDefault}
            disabled={settingDefault || !agent.enabled}
            title={!agent.enabled ? t('agents.detail.defaultNeedsEnabled') : undefined}
          >
            {settingDefault ? <Loader2 className="size-3 animate-spin" /> : <Star className="size-3" />}
            {t('agents.detail.setDefault')}
          </Button>
        )}
      </div>

      {/* Name — system agents are locked; user agents are editable via
          create-then-delete (no DB-level rename support). */}
      <Field label={t('agents.detail.name')}>
        <div className="flex items-center gap-2 rounded-lg border border-border-strong bg-surface-2 px-3 py-2">
          <input
            value={name}
            onChange={(e) => {
              markFieldEdit('name');
              setName(e.target.value);
            }}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
              if (e.key === 'Escape') cancelFieldEdit('name', serverSnapshotRef.current.name);
            }}
            disabled={locked || renaming}
            title={lockHint}
            className="flex-1 bg-transparent text-[13px] font-medium text-foreground outline-none disabled:cursor-not-allowed disabled:opacity-70"
          />
          {!locked && <Pencil className="size-3 shrink-0 text-muted" />}
        </div>
      </Field>

      {/* Description — free-text summary of what the agent is for. Feeds the
          list-row subtitle (model · effort · description). Locked for system
          agents (same as the name); editable for user agents. */}
      <Field label={t('agents.detail.description')}>
        <Textarea
          value={description}
          onChange={(e) => {
            markFieldEdit('description');
            setDescription(e.target.value);
          }}
          onBlur={() => {
            if (locked) return;
            const canonical = description.trim();
            setDescription(canonical);
            if (canonical === serverSnapshotRef.current.description && pendingRevisionRef.current.description === null) {
              cancelFieldEdit('description', canonical);
              return;
            }
            markFieldSubmitted('description');
              consumeBackgroundMutation(submitFields({ description: canonical || null }, ['description']));
          }}
          disabled={locked}
          title={lockHint}
          rows={2}
          placeholder={t('agents.detail.descriptionPlaceholder')}
          className="text-[13px] disabled:cursor-not-allowed disabled:opacity-70"
        />
      </Field>

      {/* Backend (read-only) — design.pen JUopp. "creation-time only ·
          locked" hint sits inside the value chip on the right so users
          don't mistake it for a note about the field above (the name). */}
      <Field label={t('agents.detail.backend')}>
        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-3 px-3 py-2">
          <Bot className={clsx('size-3 shrink-0', BACKEND_ICON_CLASS[agent.backend as Backend] || 'text-muted')} />
          <span className={clsx('font-mono text-[12px] font-bold', BACKEND_ICON_CLASS[agent.backend as Backend] || 'text-foreground')}>
            {agent.backend}
          </span>
          <span className="text-[11px] text-muted">·</span>
          <span className="text-[11px] text-muted">{BACKEND_LABEL[agent.backend as Backend] || agent.backend} CLI</span>
          <span className="ml-auto font-mono text-[9px] text-muted">{t('agents.detail.backendLocked')}</span>
        </div>
      </Field>

      {/* Model — Combobox with chevron + searchable + custom values. Callers
          outside the current runtime policy get the Backend field's locked
          treatment. */}
      <Field label={t('agents.detail.model')}>
        {!canEdit ? (
          <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-3 px-3 py-2">
            <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground">
              {model || t('agents.noConfig')}
            </span>
            <Lock className="size-3 shrink-0 text-muted" />
          </div>
        ) : (
          <Combobox
            options={modelOptions}
            value={model}
            onValueChange={(next) => {
              const value = next.trim();
              if (!value) return;
              markFieldEdit('model');
              markFieldSubmitted('model');
              setModel(value);
              const patch: Partial<VibeAgentFull> = { model: value };
              // If the new model can't use the current effort, fall back to a
              // valid one and persist it in the same patch — otherwise the record
              // keeps an effort the model can't run (Codex P2).
              const opts = resolveEffortOptions(agent.backend, value, reasoningOptions);
              if (effort && !opts.includes(effort)) {
                // No valid option is itself a valid answer: a model whose catalog
                // row states no efforts must clear the field, not keep the old
                // value because there was nothing to replace it with.
                const fallback = opts.includes('medium') ? 'medium' : opts[0] ?? null;
                markFieldEdit('effort');
                markFieldSubmitted('effort');
                setEffort(fallback);
                patch.reasoning_effort = fallback;
              }
              consumeBackgroundMutation(
                submitFields(patch, 'reasoning_effort' in patch ? ['model', 'effort'] : ['model']),
              );
            }}
            placeholder={t('agents.detail.modelPlaceholder')}
            emptyText={t('agents.detail.modelEmpty')}
            allowCustomValue
          />
        )}
      </Field>

      {/* Reasoning effort — design.pen LsjxT. A model whose catalog row states
          no efforts has nothing to choose, so the field is absent rather than an
          empty outline the user would read as a control that failed to load. */}
      {effortOptions.length > 0 && (
      <Field label={t('agents.detail.effort')}>
        <div
          className="grid gap-0.5 rounded-lg border border-border-strong bg-surface-2 p-0.5"
          style={{ gridTemplateColumns: `repeat(${effortOptions.length}, minmax(0, 1fr))` }}
        >
          {effortOptions.map((opt) => {
            const active = effort === opt;
            return (
              <button
                key={opt}
                type="button"
                disabled={!canEdit}
                title={canEdit ? undefined : t('agents.remoteReadOnlyHint')}
                onClick={() => {
                  markFieldEdit('effort');
                  markFieldSubmitted('effort');
                  setEffort(opt);
                  consumeBackgroundMutation(submitFields({ reasoning_effort: opt }, ['effort']));
                }}
                className={clsx(
                  'truncate rounded-md px-1 py-1.5 text-[11px] capitalize transition disabled:cursor-not-allowed',
                  active ? 'bg-mint-soft font-bold text-mint-ink' : 'font-medium text-muted hover:text-foreground',
                  !canEdit && !active && 'opacity-70 hover:text-muted',
                )}
              >
                {opt}
              </button>
            );
          })}
        </div>
      </Field>
      )}

      {/* System prompt — design.pen y3mRv: collapsed by default. Token
          estimate (cheap heuristic, see lib/tokenEstimate) replaces the
          old character count so it's actually useful for budgeting. The
          textarea-level hint was deleted because the field label + the
          chevron row already tell the user what this is. */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setSystemPromptOpen((prev) => !prev)}
            className="flex flex-1 items-center gap-2.5 rounded-lg border border-border bg-foreground/[0.015] px-3 py-2.5 text-left transition hover:bg-foreground/[0.04]"
          >
            <ChevronRight
              className={clsx(
                'size-3 shrink-0 text-muted transition-transform',
                systemPromptOpen && 'rotate-90',
              )}
            />
            <span className="flex-1 text-[12px] font-semibold text-foreground">
              {t('agents.detail.systemPrompt')}
            </span>
            <span className="font-mono text-[10px] text-muted">
              {t('agents.detail.systemPromptCount', { count: systemPromptTokens })}
            </span>
          </button>
          {/* Expand into the full editor modal (large input + Markdown
              edit/preview) — the shared EditorDialog primitive. */}
          {canEdit && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-9 shrink-0 text-muted hover:text-foreground"
              onClick={() => {
                editorClosingRef.current = false;
                editorDirtyRef.current = false;
                editorDraftRef.current = systemPrompt;
                editorBaselineRef.current = systemPrompt;
                setEditorOpen(true);
              }}
              aria-label={t('agents.detail.systemPromptExpand')}
              title={t('agents.detail.systemPromptExpand')}
            >
              <Maximize2 className="size-3.5" />
            </Button>
          )}
        </div>
        {systemPromptOpen && (
          <Textarea
            value={systemPrompt}
            onChange={(e) => {
              markFieldEdit('systemPrompt');
              setSystemPrompt(e.target.value);
            }}
            onBlur={() => {
              if (!canEdit) return;
              const canonical = systemPrompt.trim();
              setSystemPrompt(canonical);
              if (canonical === serverSnapshotRef.current.systemPrompt && pendingRevisionRef.current.systemPrompt === null) {
                cancelFieldEdit('systemPrompt', canonical);
                return;
              }
              markFieldSubmitted('systemPrompt');
              consumeBackgroundMutation(submitFields({ system_prompt: canonical || null }, ['systemPrompt']));
            }}
            readOnly={!canEdit}
            title={canEdit ? undefined : t('agents.remoteReadOnlyHint')}
            rows={6}
            placeholder={t('agents.create.systemPromptPlaceholder')}
            className="text-[12px]"
          />
        )}
      </div>

      {/* Footer — Run on the left, Delete on the right. The Disable
          button was redundant with the top Enable toggle and was
          removed. */}
      <div className="flex items-center gap-2 pt-2">
        {canEdit ? (
          <>
            <Button
              type="button"
              variant="outline"
              size="xs"
              onClick={() => setRunning(true)}
              className="border-mint/40 bg-mint-soft text-mint-ink hover:brightness-110"
            >
              <Play className="size-3" />
              {t('agents.detail.run')}
            </Button>
            <div className="flex-1" />
            {!system ? (
              <Button
                type="button"
                variant="destructive-soft"
                size="xs"
                onClick={onDelete}
              >
                <Trash2 className="size-3" />
                {t('common.delete')}
              </Button>
            ) : (
              <span className="text-[10px] text-muted">{t('agents.detail.systemLocked')}</span>
            )}
          </>
        ) : (
          <span className="text-[10px] text-muted">{t('agents.remoteReadOnlyHint')}</span>
        )}
      </div>

      {canEdit && running && <RunAgentDialog agent={agent} onClose={() => setRunning(false)} />}

      {/* Full-screen system-prompt editor — large input + Markdown preview.
          Opening from collapsed or expanded both jump straight here. */}
      <EditorDialog
        key={`${agent.id}:${editorSeedRevision}`}
        open={canEdit && editorOpen}
        onClose={() => {
          editorClosingRef.current = true;
          editorDraftRef.current = systemPrompt;
          editorBaselineRef.current = systemPrompt;
          editorDirtyRef.current = false;
          setEditorOpen(false);
        }}
        title={t('agents.detail.systemPrompt')}
        description={t('agents.detail.systemPromptEditorHint')}
        value={systemPrompt}
        placeholder={t('agents.create.systemPromptPlaceholder')}
        footerHint={(draft) => {
          if (!editorClosingRef.current) editorDirtyRef.current = draft !== editorBaselineRef.current;
          editorDraftRef.current = draft;
          return t('agents.detail.systemPromptCount', { count: estimateTokens(draft) });
        }}
        onSave={(next) => {
          const canonical = next.trim();
          markFieldEdit('systemPrompt');
          markFieldSubmitted('systemPrompt');
          setSystemPrompt(canonical);
          if (canonical !== serverSnapshotRef.current.systemPrompt || pendingRevisionRef.current.systemPrompt !== null) {
            return submitFields({ system_prompt: canonical || null }, ['systemPrompt']);
          }
          return;
        }}
      />
    </div>
  );
};

const Field: React.FC<{ label: string; labelRight?: React.ReactNode; children: React.ReactNode }> = ({
  label,
  labelRight,
  children,
}) => (
  <div className="flex flex-col gap-1.5">
    <div className="flex items-center justify-between gap-2">
      <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{label}</span>
      {labelRight}
    </div>
    {children}
  </div>
);
