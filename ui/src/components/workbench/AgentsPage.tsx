import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  ChevronRight,
  ExternalLink,
  Lock,
  LockKeyhole,
  Loader2,
  Maximize2,
  MessageSquare,
  Pencil,
  Play,
  ShieldCheck,
  Star,
  Trash2,
  UserMinus,
  UserPlus,
  X,
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
import { useBots } from '../../context/BotsContext';
import { BotAvatar } from '../ui/bot-avatar';
import { botRole, botSessionPath, eligibleForBot, liveBotAgentNames } from '../../lib/bots';
import { RemoveBotDialog } from './bots/RemoveBotDialog';
import { AgentGlyph, AgentsPageView } from './AgentsPageView';
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
import { onPageReactivated } from '../../lib/pageActivity';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { estimateTokens } from '../../lib/tokenEstimate';
import { loadBackendModelsWithRefresh, modelOptionLabel, supersedesHubAnswer, useAddModelExit, type BackendModels } from '../../lib/backendModels';
import { compatibleEffort, refusedSavedEffort, resolveEffortOptions } from '../../lib/effortOptions';
// Backend order / labels / accent classes live in lib/backendAccent, shared
// with the Skills surface (BACKEND_TEXT is this page's old BACKEND_ICON_CLASS).
import { BACKEND_LABEL, type Backend } from '../../lib/backendAccent';
import { getBackendUiMeta, type NativeCliBackend } from '@/lib/agentBackends';
import { isBuiltinAgent } from '../onboarding/setupTargets';
import { modelsApi } from '../settings/models/modelsApi';
import { agentsWithoutModel } from '../settings/models/supply';
import type { EffortMenu } from '../settings/models/types';
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
    enabled: botsEnabled,
    bots: liveBots,
    listed: botsListed,
    displayName: botDisplayName,
    openProfile: openBotProfile,
  } = useBots();
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
  // ``?agent=`` names the Agent a contextual caller wants open (Model Hub's
  // "Agent needs attention" list). Like ``?tab=`` it is a destination rather
  // than a remembered choice, so it wins over the default auto-selection.
  const agentParam = searchParams.get('agent');
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
  // Agents the gateway cannot run for want of a model, read again whenever the list changes.
  const [withoutModel, setWithoutModel] = useState<ReadonlySet<string>>(() => new Set());
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
  const [importing, setImporting] = useState<NativeCliBackend | null>(null);
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
  // Running an Agent is a use operation, editing its definition is management:
  // `POST /api/sessions` admits an editor, so Run follows chat + Agent use while
  // every definition field stays read-only.
  const canRunAgents = capabilities.can_chat && capabilities.can_use_agents;
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

  // The Models page's per-Agent projection: an Agent shown as enabled whose next turn fails
  // for want of a model says so here too. A role that cannot read it sees no marker.
  useEffect(() => {
    let active = true;
    modelsApi.listAgents().then(
      (supplies) => {
        if (active) setWithoutModel(new Set(agentsWithoutModel(supplies).map((named) => named.name)));
      },
      () => {},
    );
    return () => {
      active = false;
    };
  }, [agents]);

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
    // A contextual caller's ``?agent=`` is an explicit destination; the default
    // is only a fallback for an empty detail panel, so it must not spend a read
    // on an Agent the param is about to supersede.
    if (agentParam && available.some((agent) => agent.name === agentParam)) return;
    const target = (defaultName && available.find((a) => a.name === defaultName)) || available[0];
    if (target) void selectAgent(target.name, false, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultName, agents, agentParam]);


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
      // A Bot is Agent metadata (docs/plans/bots.md D1): its badge and locks ride
      // on the Agent rows, so a Bot change is a definitions change here.
      onBotsChanged: () => {
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

  // Applied once per value: the list arrives after mount, so this waits for the
  // named Agent to exist rather than sending a read for a name the page cannot
  // show — and a later user pick is not yanked back to the param.
  const appliedAgentParamRef = useRef<string | null>(null);
  useEffect(() => {
    if (!agentParam) {
      appliedAgentParamRef.current = null;
      return;
    }
    if (appliedAgentParamRef.current === agentParam) return;
    if (!agents.some((agent) => agent.name === agentParam)) return;
    appliedAgentParamRef.current = agentParam;
    void selectAgent(agentParam, true);
  }, [agentParam, agents, selectAgent]);

  const dismissSelected = useCallback(() => {
    const coordinator = selectedCoordinatorRef.current;
    beginSelectedIntent(null);
    coordinator.autoSelectDismissed = true;
    coordinator.autoSelectReason = null;
    commitSelected(null);
    setDetailOpen(false);
  }, [beginSelectedIntent, commitSelected]);

  // Bots and Agents are two concepts (docs/plans/bots.md D1): a Bot's Agent is
  // never listed as an Agent. The live Bot list decides once it has answered, so a
  // Bot added or removed since the Agent rows were read moves its Agent at once.
  const botAgents = useMemo(() => liveBotAgentNames({ bots: liveBots, listed: botsListed }), [liveBots, botsListed]);
  const listedAgents = useMemo(
    () => agents.filter((agent) => (botAgents ? !botAgents.has(agent.name) : !agent.bot)),
    [agents, botAgents],
  );

  // Text search and the backend filter apply to both sections. Bots all run on
  // Vibey, so a filter for another backend leaves none.
  const query = search.trim().toLowerCase();
  const filteredAgents = useMemo(
    () =>
      listedAgents.filter((agent) => {
        if (backendFilter !== 'all' && agent.backend !== backendFilter) return false;
        if (!query) return true;
        return (
          agent.name.toLowerCase().includes(query) ||
          (agent.description ?? '').toLowerCase().includes(query) ||
          (agent.model ?? '').toLowerCase().includes(query)
        );
      }),
    [listedAgents, query, backendFilter],
  );
  const filteredBots = useMemo(() => {
    if (backendFilter !== 'all' && backendFilter !== 'vibey') return [];
    if (!query) return liveBots;
    return liveBots.filter((bot) =>
      [botDisplayName(bot), bot.role, bot.handle].some((text) => text.toLowerCase().includes(query)),
    );
  }, [liveBots, botDisplayName, query, backendFilter]);

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

  const onImport = async (from: NativeCliBackend) => {
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

  return (
    <>
      <AgentsPageView
        tabs={visibleTabs}
        activeTab={activeTab}
        runningCount={runningActiveCount}
        onSelectTab={selectAgentsTab}
        running={capabilities.can_use_agents && activeTab === 'running' ? <AgentGraphTab /> : null}
        onboarding={
          canOnboardAgents && onboardingInventory ? (
            <OrganizationAgentOnboarding
              inventory={onboardingInventory}
              expanded={onboardingExpanded}
              submitting={onboardingSubmitting}
              onExpandedChange={setOnboardingExpanded}
              onOnboard={onOnboardAgents}
            />
          ) : null
        }
        loading={loading}
        onRefresh={() => {
          void refresh();
          void refreshOnboarding();
        }}
        canEdit={canEditAgents}
        onGlobalPrompts={() => setShowGlobalPrompts(true)}
        search={search}
        onSearchChange={setSearch}
        backendFilter={backendFilter}
        onBackendFilterChange={setBackendFilter}
        bots={botsEnabled ? filteredBots : null}
        botCount={liveBots.length}
        agentCount={listedAgents.length}
        onAddBot={() => openBotProfile({ kind: 'create' })}
        agents={filteredAgents}
        defaultAgentName={defaultName}
        withoutModel={withoutModel}
        selectedName={selected?.name ?? null}
        onSelect={(name) => void selectAgent(name, true)}
        importing={importing}
        onImport={onImport}
        onNewAgent={() => setShowNew(true)}
        error={error}
        detailOpen={detailOpen}
        detail={
          selected ? (
            <AgentDetailPanel
              key={selected.id}
              agent={selected}
              isDefault={defaultName === selected.name}
              canEdit={canEditAgents}
              canRun={canRunAgents}
              onChange={updateField}
              onSetDefault={onSetDefault}
              onRename={onRename}
              onDelete={onDelete}
              onClose={dismissSelected}
            />
          ) : null
        }
      />
      {canEditAgents && (
        <>
          <NewAgentDialog open={showNew} onClose={() => setShowNew(false)} onCreated={onCreated} />
          <GlobalPromptsDialog open={showGlobalPrompts} onClose={() => setShowGlobalPrompts(false)} />
        </>
      )}
    </>
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

  // The summary is a card, matching the agent detail panel's rounded-2xl +
  // strong-border shell, rather than a full-bleed band with only edge rules.
  return (
    <section className={clsx('rounded-2xl border border-border-strong bg-surface-2/60 p-4', className)}>
      <div className="flex flex-col gap-4">
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
                      {getBackendUiMeta(agent.backend).label} · {system ? t('agents.onboarding.system') : t('agents.onboarding.custom')}
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

interface DetailProps {
  agent: VibeAgentFull;
  isDefault: boolean;
  /** Definition-management authority: rename, model, prompt, default, delete. */
  canEdit: boolean;
  /** Use authority: start a Project session with this Agent. Independent of
   *  `canEdit` — an editor runs an Agent whose definition they cannot touch. */
  canRun: boolean;
  onChange: (patch: VibeAgentUpdatePayload) => Promise<void>;
  onSetDefault: () => Promise<void>;
  onRename: (newName: string) => Promise<void>;
  onDelete: () => void;
  onClose: () => void;
}

// The selected Bot's or Agent's panel (ui/design: agents/bots-redesign/bot,
// agents/bots-redesign/agent, agents/bots-redesign/default-agent). A Bot shows its
// face, name, and role, the way into its conversation, and Remove; an Agent shows
// its name as the title (renamed in place: the backend renames the row and its
// references atomically), its backend, an "On" switch where it can be turned off,
// and its description. Both then edit the model, effort, and system prompt. Only
// what applies is drawn; a field the reader cannot change reads as text, and Run
// follows `canRun`, so a read-only definition can still be launched by whoever may use it.
export const AgentDetailPanel: React.FC<DetailProps> = ({ agent, isDefault, canEdit, canRun, onChange, onSetDefault, onRename, onDelete, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  // System agents are locked everywhere; remote access locks every agent.
  const locked = isSystemAgent(agent) || !canEdit;
  const system = isSystemAgent(agent);
  const navigate = useNavigate();
  const bots = useBots();
  // A Bot's Agent is drawn as the Bot (docs/plans/bots.md D1): the live Bot list
  // decides once it has answered, so a Bot removed here turns back into an Agent.
  const liveBotAgents = liveBotAgentNames(bots);
  const isBot = liveBotAgents ? liveBotAgents.has(agent.name) : Boolean(agent.bot);
  const bot = isBot ? bots.botForAgent(agent.name) : undefined;
  const [removingBot, setRemovingBot] = useState(false);
  // While an Agent is a Bot it stays enabled and keeps its name and row; the
  // server refuses those writes, so they are not offered.
  const nameLocked = locked || isBot;
  // The built-in Agent of the built-in backend is part of the platform: it stays enabled.
  const alwaysEnabled = agent.source === 'builtin' && isBuiltinAgent(agent) && getBackendUiMeta(agent.backend).builtin;
  // Only what applies is drawn (ui/design: agents/bots-redesign/agent): no switch
  // that cannot turn, no default for a Bot or a disabled Agent, no Bot for the default.
  const canToggle = canEdit && !alwaysEnabled && !isBot;
  const canSetDefault = canEdit && !isBot && !isDefault && agent.enabled;
  const canSetBot = canEdit && bots.enabled && !isDefault && eligibleForBot(agent);
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
        effortMenus?: Record<string, EffortMenu>;
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
  const openAddModel = useAddModelExit(agent.backend, canEdit);
  const surfaceActive = useRouteSurfaceActive();

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
  // backend doesn't know about yet (e.g. a freshly-released preview). Read
  // again when Settings uncovers the page: the Model Hub there edits the list,
  // so the rows read before the visit go when Settings covers it.
  const applyModelCatalog = useCallback((backend: string, { models, modelLabels, reasoningOptions: opts, effortMenus }: BackendModels) => {
    setModelCatalogs((current) => ({
      ...current,
      [backend]: {
        modelOptions: models.map((m) => ({ value: m, label: modelOptionLabel(m, modelLabels) })),
        reasoningOptions: opts ?? {},
        effortMenus,
      },
    }));
  }, []);
  // Whether this backend answered from Model Hub in this activation; only another
  // Hub answer replaces a Hub one (`supersedesHubAnswer`).
  const hubKnownRef = useRef(false);
  const takeModelCatalog = useCallback((backend: string, result: BackendModels) => {
    if (!supersedesHubAnswer(result, hubKnownRef.current)) return;
    // A confirmed Direct answer ends the Hub-only re-reads; an unread Hub got here only if none was known.
    if (result.effortMenus !== undefined) hubKnownRef.current = true;
    else if (!result.hubUnread) hubKnownRef.current = false;
    applyModelCatalog(backend, result);
  }, [applyModelCatalog]);
  useEffect(() => {
    if (!surfaceActive) return;
    const cancel = loadBackendModelsWithRefresh(
      api,
      agent.backend,
      (result) => takeModelCatalog(agent.backend, result),
      () => {
        setModelCatalogs((current) => ({
          ...current,
          [agent.backend]: { modelOptions: [], reasoningOptions: {} },
        }));
      },
    );
    return () => {
      cancel();
      setModelCatalogs((current) => {
        const next = { ...current };
        delete next[agent.backend];
        return next;
      });
    };
  }, [agent.backend, api, surfaceActive, takeModelCatalog]);

  // A Hub catalog's effort menu follows capability evidence, which a turn in any
  // session can change while this panel stays open. Like the route picker, which
  // reads it as its menu opens, read it again as the user turns to the effort
  // field. Keep the buttons focusable while the read is out: disabling them
  // during pointer entry/focus swallows the click. A choice made during the
  // read waits for its answer and is validated before it can write an effort.
  const effortRefreshRef = useRef<(() => void) | null>(null);
  const effortPointerInsideRef = useRef(false);
  const effortFieldRef = useCallback((node: HTMLDivElement | null) => {
    // A model with no efforts removes the field without a pointerleave event.
    if (!node) effortPointerInsideRef.current = false;
  }, []);
  const pendingEffortChoiceRef = useRef<{ model: string; value: string } | null>(null);
  const settleEffortChoiceRef = useRef<(result?: BackendModels) => void>(() => {});
  // One read per approach: a read still out answers for this one too. Dropping a
  // read does not stop its request, so restarting would only add requests.
  const effortRefreshOutRef = useRef(false);
  const [effortRefreshOut, setEffortRefreshOut] = useState(false);
  const refreshEffortMenus = useCallback(() => {
    if (!surfaceActive || !hubKnownRef.current || effortRefreshOutRef.current) return;
    // A settled native fallback can still have catalog retries pending. Retire
    // its loader so no older answer can settle this approach's queued choice.
    effortRefreshRef.current?.();
    effortRefreshOutRef.current = true;
    setEffortRefreshOut(true);
    const backend = agent.backend;
    const settle = (result?: BackendModels) => {
      effortRefreshOutRef.current = false;
      setEffortRefreshOut(false);
      settleEffortChoiceRef.current(result);
    };
    effortRefreshRef.current = loadBackendModelsWithRefresh(
      api,
      backend,
      (result) => {
        const accepted = supersedesHubAnswer(result, hubKnownRef.current);
        takeModelCatalog(backend, result);
        settle(accepted ? result : undefined);
      },
      settle,
    );
  }, [agent.backend, api, surfaceActive, takeModelCatalog]);
  // A refresh still out when the page is covered or its backend changes answers
  // for a catalog this page no longer shows, so it is dropped.
  useEffect(() => () => {
    effortRefreshRef.current?.();
    effortRefreshRef.current = null;
    effortRefreshOutRef.current = false;
    effortPointerInsideRef.current = false;
    pendingEffortChoiceRef.current = null;
    setEffortRefreshOut(false);
    // A new activation or backend starts without knowing which catalog serves it.
    hubKnownRef.current = false;
  }, [agent.backend, api, surfaceActive]);

  const systemPromptTokens = estimateTokens(systemPrompt);
  // Effort options follow the backend + selected model when the catalog provides them.
  const effortOptions = resolveEffortOptions(agent.backend, model, reasoningOptions);
  // Routing now refuses the saved effort on every hop: out of the options, but the
  // Agent still sends it, so it shows as itself and says so.
  const refusedEffort = refusedSavedEffort(effort, model, activeModelCatalog?.effortMenus);
  // The efforts shown before the catalog answers are the backend's fallback
  // ladder, which may hold one the model rejects; they stay visible but cannot
  // be picked until the read (or its failure) lands.
  const effortsAnswered = activeModelCatalog !== undefined;
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

  const submitEffortChoice = (value: string) => {
    markFieldEdit('effort');
    markFieldSubmitted('effort');
    setEffort(value);
    consumeBackgroundMutation(submitFields({ reasoning_effort: value }, ['effort']));
  };
  useLayoutEffect(() => {
    settleEffortChoiceRef.current = (result) => {
      const choice = pendingEffortChoiceRef.current;
      pendingEffortChoiceRef.current = null;
      if (!choice || !surfaceActive || !canEdit || choice.model !== model) return;
      // An unread Hub must not replace its last accepted menu with the native
      // ladder. A failed read retains the same last-answer policy as the picker.
      const options = resolveEffortOptions(agent.backend, model, result ? result.reasoningOptions ?? {} : reasoningOptions);
      if (!options.includes(choice.value)) {
        showToast(t('agents.detail.effortUnavailable', {
          effort: choice.value === 'none' ? t('chat.picker.effortOptions.none') : choice.value,
        }), 'error');
        return;
      }
      submitEffortChoice(choice.value);
    };
  });

  // Only user Agents can be renamed; the backend moves every durable name
  // reference in the same transaction.
  const commitRename = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === agent.name) {
      cancelFieldEdit('name', serverSnapshotRef.current.name);
      return;
    }
    if (nameLocked) {
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

  const botBuiltin = bot?.builtin ?? agent.bot?.builtin ?? false;
  const botName = bot ? bots.displayName(bot) : agent.bot?.display_name || (botBuiltin ? t('bots.mainDefaultName') : agent.name);
  const botRoleText = botRole({ builtin: botBuiltin, role: agent.description ?? '' }, t('bots.mainRole'));

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
    <div className="flex flex-col gap-4">
      {isBot ? (
        // ui/design: agents/bots-redesign/bot. The Bot itself: face, name, and role,
        // and the way into its conversation. Its Agent's name is not shown.
        <>
          <div className="flex items-start gap-3">
            <BotAvatar avatar={bot?.avatar ?? null} name={botName} size={56} />
            <div className="flex min-w-0 flex-1 flex-col gap-1 pt-1">
              <div className="flex items-center gap-1.5">
                <span className="truncate text-lg font-bold text-foreground">{botName}</span>
                {botBuiltin && (
                  <Badge variant="success" className="shrink-0 px-1.5 py-0">
                    {t('bots.mainBadge')}
                  </Badge>
                )}
              </div>
              {botRoleText && <p className="text-xs leading-relaxed text-muted">{botRoleText}</p>}
            </div>
            <PanelClose label={t('common.close')} onClose={onClose} />
          </div>
          {bot && (
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" variant="outline" size="xs" className={MINT_ACTION} onClick={() => navigate(botSessionPath(bot))}>
                <MessageSquare className="size-3" />
                {t('bots.agents.openConversation')}
              </Button>
              {canEdit && (
                <Button type="button" variant="outline" size="xs" onClick={() => bots.openProfile({ kind: 'edit', handle: bot.handle })}>
                  <Pencil className="size-3" />
                  {t('bots.agents.editProfile')}
                </Button>
              )}
            </div>
          )}
          <div className="h-px bg-border" />
        </>
      ) : (
        // ui/design: agents/bots-redesign/agent. The name is the title, renamed in
        // place unless it is locked; the backend is its subtitle; "On" is a switch
        // only where the Agent can be turned off.
        <>
          <div className="flex items-start gap-3">
            <AgentGlyph backend={agent.backend} size="lg" />
            <div className="flex min-w-0 flex-1 flex-col gap-0.5 pt-0.5">
              <div className="flex min-w-0 items-center gap-1.5">
                {nameLocked ? (
                  <span className="truncate text-lg font-bold text-foreground">{agent.name}</span>
                ) : (
                  <>
                    <input
                      value={name}
                      size={Math.max(name.length, 1)}
                      aria-label={t('agents.detail.name')}
                      onChange={(e) => {
                        markFieldEdit('name');
                        setName(e.target.value);
                      }}
                      onBlur={commitRename}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                        if (e.key === 'Escape') cancelFieldEdit('name', serverSnapshotRef.current.name);
                      }}
                      disabled={renaming}
                      className="field-sizing-content min-w-0 max-w-full rounded-md bg-transparent text-lg font-bold text-foreground outline-none transition hover:bg-surface-2 focus:bg-surface-2 focus:ring-2 focus:ring-ring"
                    />
                    <Pencil className="size-3 shrink-0 text-muted" aria-hidden="true" />
                  </>
                )}
                {isDefault && (
                  <Badge variant="success" className="shrink-0 px-1.5 py-0">
                    {t('common.default')}
                  </Badge>
                )}
              </div>
              <span className="text-xs text-muted">{getBackendUiMeta(agent.backend).label}</span>
            </div>
            {canToggle && (
              <span className="flex shrink-0 items-center gap-2 pt-1.5 text-xs text-muted">
                {t('agents.detail.on')}
                <Switch
                  size="sm"
                  checked={agent.enabled}
                  onCheckedChange={(next) => {
                    consumeBackgroundMutation(onChange({ enabled: next }));
                  }}
                  label={t('agents.detail.enabled')}
                />
              </span>
            )}
            <PanelClose label={t('common.close')} onClose={onClose} />
          </div>

          {/* What the Agent is for; it feeds the list row. A built-in Agent's reads as text. */}
          {(!locked || agent.description) && (
          <Field label={t('agents.detail.description')}>
            {locked ? (
              <p className="text-sm leading-relaxed text-foreground">{agent.description}</p>
            ) : (
              <Textarea
                value={description}
                onChange={(e) => {
                  markFieldEdit('description');
                  setDescription(e.target.value);
                }}
                onBlur={() => {
                  const canonical = description.trim();
                  setDescription(canonical);
                  if (canonical === serverSnapshotRef.current.description && pendingRevisionRef.current.description === null) {
                    cancelFieldEdit('description', canonical);
                    return;
                  }
                  markFieldSubmitted('description');
                  consumeBackgroundMutation(submitFields({ description: canonical || null }, ['description']));
                }}
                rows={2}
                placeholder={t('agents.detail.descriptionPlaceholder')}
                className="text-sm"
              />
            )}
          </Field>
          )}
        </>
      )}

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
              // Choosing the current model again is no edit: nothing to write, and a
              // refused saved effort stays what it is.
              if (!value || value === model) return;
              markFieldEdit('model');
              markFieldSubmitted('model');
              setModel(value);
              const patch: Partial<VibeAgentFull> = { model: value };
              // If the new model can't use the current effort, fall back to a
              // valid one and persist it in the same patch — otherwise the record
              // keeps an effort the model can't run (Codex P2).
              const opts = resolveEffortOptions(agent.backend, value, reasoningOptions);
              if (effort && !opts.includes(effort)) {
                const fallback = compatibleEffort(effort, opts);
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
            disabled={effortRefreshOut}
            footerAction={openAddModel ? { label: t('chat.picker.addModel'), onSelect: openAddModel } : undefined}
          />
        )}
      </Field>

      {/* Reasoning effort. A model whose catalog row states
          no efforts has nothing to choose, so the field is absent rather than an
          empty outline the user would read as a control that failed to load. */}
      {(effortOptions.length > 0 || refusedEffort) && (
      <Field
        label={t('agents.detail.effort')}
        labelRight={effortRefreshOut ? <Loader2 aria-label={t('common.loading')} className="size-3 animate-spin text-muted" /> : undefined}
      >
        <div
          className="grid gap-0.5 rounded-lg border border-border-strong bg-surface-2 p-0.5"
          style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(3.25rem, 1fr))' }}
          data-effort-field
          ref={effortFieldRef}
          aria-busy={effortRefreshOut}
          onPointerEnter={() => {
            effortPointerInsideRef.current = true;
            refreshEffortMenus();
          }}
          onPointerLeave={() => { effortPointerInsideRef.current = false; }}
          // Pointer-induced focus belongs to the approach already refreshed,
          // even if that read finished before the pointer was pressed.
          onFocusCapture={(event) => {
            if (!effortPointerInsideRef.current && !event.currentTarget.contains(event.relatedTarget as Node | null)) refreshEffortMenus();
          }}
        >
          {refusedEffort && (
            <span
              title={t('chat.picker.effortRefused')}
              className="truncate rounded-md bg-gold/10 px-1 py-1.5 text-center text-[11px] font-bold capitalize text-gold-ink"
            >
              {refusedEffort === 'none' ? t('chat.picker.effortOptions.none') : refusedEffort}
            </span>
          )}
          {effortOptions.map((opt) => {
            const active = effort === opt;
            return (
              <button
                key={opt}
                type="button"
                disabled={!canEdit || !effortsAnswered}
                title={canEdit ? undefined : t('agents.remoteReadOnlyHint')}
                onClick={() => {
                  if (effortRefreshOutRef.current) {
                    pendingEffortChoiceRef.current = { model, value: opt };
                    return;
                  }
                  submitEffortChoice(opt);
                }}
                className={clsx(
                  'truncate rounded-md px-1 py-1.5 text-[11px] capitalize transition disabled:cursor-not-allowed',
                  active ? 'bg-mint-soft font-bold text-mint-ink' : 'font-medium text-muted hover:text-foreground',
                  !canEdit && !active && 'opacity-70 hover:text-muted',
                )}
              >
                {opt === 'none' ? t('chat.picker.effortOptions.none') : opt}
              </button>
            );
          })}
        </div>
        {refusedEffort && <span className="text-[11px] text-gold-ink">{t('chat.picker.effortRefused')}</span>}
      </Field>
      )}

      {/* System prompt: collapsed by default. Token
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

      {isBot ? (
        // The main Bot is never removed, and its handle is never typed: it has no footer.
        !botBuiltin && (
          <div className="flex items-center gap-2 border-t border-border pt-4">
            <span className="font-mono text-xs text-muted" title={t('bots.handleHint')}>
              {t('bots.handle', { handle: bot?.handle ?? agent.bot?.handle ?? agent.name })}
            </span>
            <div className="flex-1" />
            {canEdit && bot && (
              <Button type="button" variant="destructive-soft" size="xs" onClick={() => setRemovingBot(true)}>
                <UserMinus className="size-3" />
                {t('bots.menu.remove')}
              </Button>
            )}
          </div>
        )
      ) : (
        (canRun || canSetDefault || canSetBot || (canEdit && !system)) && (
          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
            {canRun && (
              <Button type="button" variant="outline" size="xs" onClick={() => setRunning(true)} className={MINT_ACTION}>
                <Play className="size-3" />
                {t('agents.detail.run')}
              </Button>
            )}
            {canSetDefault && (
              <Button type="button" variant="ghost" size="xs" onClick={handleSetDefault} disabled={settingDefault}>
                {settingDefault ? <Loader2 className="size-3 animate-spin" /> : <Star className="size-3" />}
                {t('agents.detail.setDefault')}
              </Button>
            )}
            {canSetBot && (
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={() => bots.openProfile({ kind: 'create', tab: 'agent', agentName: agent.name })}
              >
                <UserPlus className="size-3" />
                {t('bots.agents.setAsBot')}
              </Button>
            )}
            <div className="flex-1" />
            {canEdit && !system && (
              <Button type="button" variant="destructive-soft" size="xs" onClick={onDelete}>
                <Trash2 className="size-3" />
                {t('common.delete')}
              </Button>
            )}
          </div>
        )
      )}

      {bot && <RemoveBotDialog bot={bot} open={removingBot} onOpenChange={setRemovingBot} />}

      {/* Derived, not latched: losing `canRun` mid-dialog unmounts it in the same
          render, so a revoked capability cannot leave an open launcher behind. */}
      {canRun && running && <RunAgentDialog agent={agent} onClose={() => setRunning(false)} />}

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

const MINT_ACTION = 'border-mint/40 bg-mint-soft text-mint-ink hover:brightness-110';

const PanelClose: React.FC<{ label: string; onClose: () => void }> = ({ label, onClose }) => (
  <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label={label} className="size-7 shrink-0">
    <X className="size-3.5" />
  </Button>
);

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
