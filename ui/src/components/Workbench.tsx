import { useEffect, useRef, useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { ChevronDown, FolderOpen, ListChecks, Search, Smartphone } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { useNewSession } from '../lib/useNewSession';
import { NewProjectDialog } from './workbench/NewProjectDialog';
import { Composer, type ComposerHandle } from './workbench/Composer';
import { ProjectPicker } from './workbench/ProjectPicker';
import { AgentRoutePicker } from './workbench/AgentRoutePicker';
import { ReadyBanner } from './workbench/ReadyBanner';
import { shouldShowReadyBanner, useBackendReadiness } from './workbench/backendReadiness';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { canCreateLocalProject } from '../lib/sessionInfo';
import logoImg from '../assets/logo.png';
import type { TranslationKey } from '../i18n/types';

// The three starting points the home offers (design LDlvV). Each one seeds the
// draft and focuses the input — it never sends, so the user edits a concrete
// sentence instead of staring at an empty box.
const SUGGESTIONS: {
  key: string;
  icon: LucideIcon;
  titleKey: TranslationKey;
  bodyKey: TranslationKey;
  draftKey: TranslationKey;
}[] = [
  {
    key: 'explore',
    icon: FolderOpen,
    titleKey: 'workbench.home.suggestions.exploreTitle',
    bodyKey: 'workbench.home.suggestions.exploreBody',
    draftKey: 'workbench.home.suggestions.exploreDraft',
  },
  {
    key: 'research',
    icon: Search,
    titleKey: 'workbench.home.suggestions.researchTitle',
    bodyKey: 'workbench.home.suggestions.researchBody',
    draftKey: 'workbench.home.suggestions.researchDraft',
  },
  {
    key: 'plan',
    icon: ListChecks,
    titleKey: 'workbench.home.suggestions.planTitle',
    bodyKey: 'workbench.home.suggestions.planBody',
    draftKey: 'workbench.home.suggestions.planDraft',
  },
];

// The first-task home (design SfdNo + TfqkD + CRERw): one question, three
// starting points, and the shared chat Composer carrying the Agent and workspace
// pickers on its own action row. Sending creates a session under the selected
// workspace with the selected Agent and routes to /chat/<id> with the typed
// message pre-seeded; no workspace surfaces the project dialog so the user gets
// unstuck in place. The create flow lives in the shared useNewSession hook — one
// source of truth with the mobile NewSessionSheet.
export const Workbench: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { capabilities } = useInstanceAuthorization();
  const canCreateProject = canCreateLocalProject(capabilities);
  const ns = useNewSession({
    active: capabilities.can_chat,
    loadErrorText: t('newSession.loadError'),
    createFailedText: t('newSession.createFailed'),
  });
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false);
  const composerRef = useRef<ComposerHandle>(null);

  // The wizard hands `{ onboardingCompleted: true }` to this route. That is an
  // event, not a property of the location — history state survives a reload, so
  // leaving the key in place would re-announce the same setup every time the
  // user came back here. Latch it for this visit (a readiness answer can still
  // arrive later) and replace the history entry with the REST of its state, so
  // the overlay origin and anything else travelling with it stays intact.
  // The backend itself must still confirm it is ready, and that producer is not
  // on this branch yet (see useBackendReadiness), so today nothing renders.
  const wizardState = location.state as Record<string, unknown> | null;
  const wizardJustFinished = Boolean(wizardState?.onboardingCompleted);
  const [onboardingCompleted, setOnboardingCompleted] = useState(wizardJustFinished);
  const [bannerDismissed, setBannerDismissed] = useState(false);
  // Adjusted during render rather than in an effect (the codebase's effect-free
  // pattern), guarded so it cannot loop: the key can also arrive at an already
  // mounted home, and latching it a frame later would race the replace below.
  if (wizardJustFinished && !onboardingCompleted) setOnboardingCompleted(true);
  useEffect(() => {
    if (!wizardState?.onboardingCompleted) return;
    const { onboardingCompleted: _consumed, ...rest } = wizardState;
    navigate(`${location.pathname}${location.search}${location.hash}`, {
      replace: true,
      state: Object.keys(rest).length > 0 ? rest : null,
    });
  }, [location.hash, location.pathname, location.search, wizardState, navigate]);
  const currentBackend = ns.agentRoute.agent_backend
    ?? ns.agents.find((agent) => agent.name === ns.agentRoute.agent_name)?.backend
    ?? null;
  const readiness = useBackendReadiness(currentBackend);
  const showReadyBanner = shouldShowReadyBanner({
    onboardingCompleted,
    readiness,
    currentBackend,
    dismissed: bannerDismissed,
  });

  // Returns whether the send actually started, so the Composer only clears the
  // box on a real start — a no-project nudge or a transient create error keeps
  // the typed prompt for retry. Navigation stays here (the hook is router-free).
  const send = async (text: string): Promise<boolean> => {
    const result = await ns.send(text);
    if (result) {
      // Hand the typed message to ChatPage as router state; it replays it
      // through the fire-and-forget compose path so the agent turn starts.
      navigate(`/chat/${encodeURIComponent(result.sessionId)}`, { state: { initialMessage: result.initialMessage } });
      return true;
    }
    if (text.trim() && ns.needsProject) setNewProjectOpen(true);
    return false;
  };

  if (!capabilities.can_chat) return <Navigate to="/projects" replace />;

  const workspaceLabel = ns.target?.display_name ?? t('workbench.home.chooseWorkspace');
  const workspaceChipClass = 'flex h-7 min-w-0 max-w-[220px] items-center gap-1.5 rounded-md px-2 text-[12px] text-muted transition-colors hover:bg-foreground/[0.06] hover:text-foreground disabled:pointer-events-none disabled:opacity-50';
  const workspaceChipBody = (
    <>
      <span className="min-w-0 truncate">{workspaceLabel}</span>
      <ChevronDown className="size-3 shrink-0" />
    </>
  );
  // One affordance, two contents by capability. Opening folders is how a
  // workspace is chosen (design: the chip opens the directory browser directly,
  // with no menu in between); someone who cannot open folders still has to be
  // able to pick among the projects they already have, so the same chip lists
  // them instead of becoming a control that does nothing.
  const workspaceChip = canCreateProject ? (
    <button
      type="button"
      onClick={() => setNewProjectOpen(true)}
      disabled={ns.sending}
      title={ns.target?.folder_path}
      aria-label={ns.target ? t('workbench.home.workspaceAria', { path: ns.target.folder_path }) : t('workbench.home.chooseWorkspace')}
      className={workspaceChipClass}
    >
      {workspaceChipBody}
    </button>
  ) : (
    <Popover open={workspaceMenuOpen} onOpenChange={setWorkspaceMenuOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={ns.sending || ns.projects.length === 0}
          title={ns.target?.folder_path}
          aria-label={ns.target ? t('workbench.home.workspaceAria', { path: ns.target.folder_path }) : t('workbench.home.chooseWorkspace')}
          className={workspaceChipClass}
        >
          {workspaceChipBody}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[320px] p-2">
        <ProjectPicker
          projects={ns.projects}
          targetId={ns.target?.id}
          onSelect={(id) => {
            ns.setSelected(id);
            setWorkspaceMenuOpen(false);
          }}
          onNewProject={() => setNewProjectOpen(true)}
          disabled={ns.sending}
        />
      </PopoverContent>
    </Popover>
  );

  return (
    // Desktop: the discovery block is centred in whatever height is left and the
    // composer sits at the bottom (design rhythm — not a fixed block plus a fixed
    // gap). Mobile DOESN'T get the tall centred column: it fights the iOS
    // keyboard, where top-aligned normal flow lets the focused composer scroll
    // into view the way an ordinary in-flow input does.
    //
    // The column is fluid. 856 in the source is what a 1200 staging window minus
    // the 248 sidebar and its padding happens to leave — no max width is
    // expressed anywhere in the design, so none is imposed here.
    <div className="flex w-full flex-col gap-6 md:min-h-[calc(100dvh-4rem)]">
      {showReadyBanner && currentBackend && (
        <ReadyBanner backend={currentBackend} onDismiss={() => setBannerDismissed(true)} />
      )}

      <div className="flex flex-col items-center justify-center gap-6 py-6 md:flex-1 md:py-0">
        <div className="flex flex-col items-center gap-4 text-center">
          <img src={logoImg} alt="" aria-hidden="true" className="h-12 w-16 object-contain" />
          <h1 className="text-[clamp(22px,4vw,30px)] font-semibold text-foreground">{t('workbench.home.heroTitle')}</h1>
          <p className="max-w-[520px] text-[14px] leading-[1.5] text-muted">{t('workbench.home.heroBody')}</p>
        </div>

        {/* Three starting points (design LDlvV): equal columns on desktop, stacked
            on a phone where 277-wide cards would be unreadable. */}
        <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-3">
          {SUGGESTIONS.map(({ key, icon: Icon, titleKey, bodyKey, draftKey }) => (
            <button
              key={key}
              type="button"
              onClick={() => composerRef.current?.setDraft(t(draftKey))}
              className="flex flex-col items-start gap-3 rounded-xl border border-border bg-surface-2 p-[18px] text-left transition-colors hover:border-border-strong hover:bg-foreground/[0.03]"
            >
              <Icon className="size-[19px] shrink-0 text-mint-ink" />
              <span className="flex flex-col gap-1">
                <span className="text-[13px] font-semibold text-foreground">{t(titleKey)}</span>
                <span className="text-[12px] leading-[1.5] text-muted">{t(bodyKey)}</span>
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-4 pb-2">
        <Composer
          ref={composerRef}
          onSend={send}
          placeholder={t('workbench.home.inputPlaceholder')}
          disabled={ns.sending}
          actions={
            <>
              <AgentRoutePicker
                value={ns.agentRoute}
                agents={ns.agents}
                onChange={ns.setAgentRoute}
                defaultLabel={ns.effectiveDefaultAgentName
                  ? t('newSession.defaultAgentNamed', { name: ns.effectiveDefaultAgentName })
                  : t('newSession.defaultAgent')}
                disabled={ns.sending}
                align="start"
                triggerClassName="h-7 max-w-[240px] rounded-md px-2 py-0"
              />
              {workspaceChip}
            </>
          }
        />
        {ns.needsProject && (
          <div className="px-2 text-[10.5px] text-gold-ink">{t('workbench.home.noProjectForChat')}</div>
        )}
        {ns.error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
            {ns.error}
          </div>
        )}

        {/* Continuation row (design CRERw) — where this conversation can carry on
            when the user leaves the desk. */}
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <Link
            to="/settings/remote-access"
            className="flex items-center gap-1.5 text-[12px] font-semibold text-mint-ink transition-opacity hover:opacity-80"
          >
            <Smartphone className="size-[15px] shrink-0" />
            <span>{t('workbench.home.continueOnPhone')}</span>
          </Link>
          <span className="text-[11px] text-muted">
            <Trans
              i18nKey="workbench.home.connectChatApps"
              components={{
                settings: <Link to="/settings/platforms" className="font-bold text-cyan-ink hover:underline" />,
              }}
            />
          </span>
        </div>
      </div>

      {newProjectOpen && canCreateProject && (
        <NewProjectDialog
          onClose={() => setNewProjectOpen(false)}
          onCreated={(project) => {
            setNewProjectOpen(false);
            // create_project is find-or-create by path: dedup + hoist to top so the
            // "most recent" target reflects the folder just opened.
            ns.upsertSelectProject(project);
          }}
        />
      )}
    </div>
  );
};
