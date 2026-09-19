import { useEffect, useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Bot, ChevronDown, Clock, FolderOpen, Smartphone } from 'lucide-react';

import { useRouteSurfaceActive } from '../lib/routeSurfaceActivity';
import { useNewSession } from '../lib/useNewSession';
import { NewProjectDialog } from './workbench/NewProjectDialog';
import { Composer, type ComposerAttachment } from './workbench/Composer';
import { ProjectPicker } from './workbench/ProjectPicker';
import { AgentRoutePicker } from './workbench/AgentRoutePicker';
import { ReadyBanner } from './workbench/ReadyBanner';
import { shouldShowReadyBanner, useBackendReadiness, useSetupHandoff } from './workbench/backendReadiness';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { canCreateLocalProject } from '../lib/sessionInfo';
import logoImg from '../assets/logo.png';
import { CreateViaChatDialog } from './workbench/CreateViaChatDialog';
import { Button } from './ui/button';

// The home owns one unsent draft. Files and voice stay in its Composer until
// explicit Send binds the selected project/Agent and commits the first message.
export const Workbench: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const surfaceActive = useRouteSurfaceActive();
  const [sentSessionId, setSentSessionId] = useState<string | null>(null);
  useEffect(() => {
    if (surfaceActive && sentSessionId) navigate(`/chat/${encodeURIComponent(sentSessionId)}`);
  }, [navigate, sentSessionId, surfaceActive]);
  const location = useLocation();
  const { capabilities } = useInstanceAuthorization();
  const canCreateProject = canCreateLocalProject(capabilities);
  const ns = useNewSession({
    active: capabilities.can_chat,
    loadErrorText: t('newSession.loadError'),
    createFailedText: t('newSession.createFailed'),
    errorText: t,
  });
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false);
  const [newTaskOpen, setNewTaskOpen] = useState(false);

  // The wizard hands `{ onboardingCompleted: true }` to this route. That is an
  // event, not a property of the location — history state survives a reload, so
  // leaving the key in place would re-announce the same setup every time the
  // user came back here. Take it out of the entry, keeping the REST of the state
  // so the overlay origin and anything else travelling with it stays intact, and
  // hold the event for the page (see useSetupHandoff: this home is remounted
  // mid-handoff, and a readiness answer can still arrive after that).
  // The backend itself must still confirm it is ready, so holding it alone shows
  // nothing: it only makes the question worth asking.
  const wizardState = location.state as Record<string, unknown> | null;
  const wizardJustFinished = Boolean(wizardState?.onboardingCompleted);
  const {
    completed: onboardingCompleted,
    dismissed: bannerDismissed,
    dismiss: dismissReadyBanner,
  } = useSetupHandoff(wizardJustFinished);
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
  // Asked only while there is a completion left to announce: an ordinary visit,
  // and every visit after the user has dismissed the banner, reads nothing.
  const readiness = useBackendReadiness(currentBackend, onboardingCompleted && !bannerDismissed);
  const showReadyBanner = shouldShowReadyBanner({
    onboardingCompleted,
    readiness,
    currentBackend,
    dismissed: bannerDismissed,
  });

  const send = async (text: string, attachments: ComposerAttachment[] = []): Promise<boolean> => {
    const result = await ns.send(text, attachments);
    if (result) {
      // The first POST has completed. Navigation only opens its transcript;
      // handing initialMessage to ChatPage here would start a duplicate turn.
      setSentSessionId(result.sessionId);
      return true;
    }
    if ((text.trim() || attachments.length) && ns.needsProject && canCreateProject) setNewProjectOpen(true);
    return false;
  };

  if (!capabilities.can_chat) return <Navigate to="/projects" replace />;

  const workspaceLabel = ns.target?.display_name ?? t('workbench.home.chooseWorkspace');
  const workspaceChipClass = 'flex h-7 min-w-0 max-w-full sm:max-w-[220px] items-center gap-1.5 rounded-md px-2 text-[12px] text-muted transition-colors hover:bg-foreground/[0.06] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50';
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
    // keyboard. There the block flows from the top and the composer pins itself
    // to the bottom of the scroll area instead (see below).
    //
    // The column is fluid. 856 in the source is what a 1200 staging window minus
    // the 248 sidebar and its padding happens to leave — no max width is
    // expressed anywhere in the design, so none is imposed here.
    <div className="flex w-full flex-col gap-6 md:min-h-[calc(100dvh-4rem)]">
      {showReadyBanner && currentBackend && (
        <ReadyBanner backend={currentBackend} onDismiss={dismissReadyBanner} />
      )}

      <div className="flex flex-col items-center justify-center gap-6 py-6 md:flex-1 md:py-0">
        <div className="flex flex-col items-center gap-4 text-center">
          <img src={logoImg} alt="" aria-hidden="true" className="h-12 w-16 object-contain" />
          <h1 className="text-[clamp(22px,4vw,30px)] font-semibold text-foreground">{t('workbench.home.heroTitle')}</h1>
          <p className="max-w-[520px] text-[14px] leading-[1.5] text-muted">{t('workbench.home.heroBody')}</p>
        </div>

        <div className="flex flex-wrap items-center justify-center gap-2">
          <Button variant="outline" size="sm" disabled={ns.sending}
            onClick={() => canCreateProject ? setNewProjectOpen(true) : setWorkspaceMenuOpen(true)}>
            <FolderOpen className="size-4" />{t('workbench.home.openProject')}
          </Button>
          {capabilities.can_manage_agents && (
            <Button variant="outline" size="sm" asChild>
              <Link to="/agents"><Bot className="size-4" />{t('workbench.home.manageAgents')}</Link>
            </Button>
          )}
          <Button variant="outline" size="sm" disabled={ns.sending} onClick={() => setNewTaskOpen(true)}>
            <Clock className="size-4" />{t('workbench.home.createTask')}
          </Button>
        </div>
      </div>

      {/* Keep the action row above the mobile tab bar while the content scrolls. */}
      <div className="flex flex-col gap-4 pb-2 max-md:sticky max-md:bottom-[var(--mobile-nav-clearance)] max-md:z-10 max-md:bg-background max-md:pt-3">
        {/* A card that scrolls behind an opaque block reads as cut in half. The
            short fade above it says "passing behind", which is what happens. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 -top-4 h-4 bg-gradient-to-t from-background to-transparent md:hidden"
        />
        <Composer
          stageMedia
          onSend={send}
          placeholder={t('workbench.home.inputPlaceholder')}
          disabled={ns.sending || !ns.loaded}
          sendDisabled={Boolean(ns.uncertainSessionId)}
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
                triggerClassName="h-7 min-w-0 max-w-full sm:max-w-[240px] rounded-md border-transparent bg-transparent px-2 py-0 hover:bg-foreground/[0.06]"
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
            {ns.uncertainSessionId && (
              <Link className="ml-2 underline underline-offset-2" target="_blank" rel="noopener noreferrer"
                to={`/chat/${encodeURIComponent(ns.uncertainSessionId)}`}>
                {t('newSession.inspectSession')}
              </Link>
            )}
          </div>
        )}

        {/* Continuation row (design CRERw) — where this conversation can carry on
            when the user leaves the desk. Both destinations are OWNER_ONLY_ROUTES,
            so anyone without `can_manage_instance` who followed either would be
            bounced straight back here; the whole row goes rather than the links
            alone, because the sentence around them only exists to introduce a
            destination they cannot reach. */}
        {capabilities.can_manage_instance && (
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
        )}
      </div>

      {newTaskOpen && <CreateViaChatDialog kind="task" onClose={() => setNewTaskOpen(false)} />}
      {newProjectOpen && canCreateProject && (
        <NewProjectDialog
          initialPath={ns.target?.folder_path}
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
