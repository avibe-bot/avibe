import { useEffect, useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Activity, Bot, FolderPlus, Smartphone, Sparkles } from 'lucide-react';

import { useRouteSurfaceActive } from '../lib/routeSurfaceActivity';
import { useNewSession } from '../lib/useNewSession';
import { NewProjectDialog } from './workbench/NewProjectDialog';
import { Composer, type ComposerAttachment } from './workbench/Composer';
import { ProjectPicker } from './workbench/ProjectPicker';
import { AgentRoutePicker } from './workbench/AgentRoutePicker';
import { ReadyBanner } from './workbench/ReadyBanner';
import { shouldShowReadyBanner, useBackendReadiness, useSetupHandoff } from './workbench/backendReadiness';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { canCreateLocalProject } from '../lib/sessionInfo';
import { CreateViaChatDialog } from './workbench/CreateViaChatDialog';

// The pill treatment the approved home puts under the heading. Disabled styling
// is this home's own: the reference never shows a send in flight, but a pill
// that stays lit while it is inert would be lying.
const SUGGESTION_PILL_CLASS = 'group flex items-center gap-2 rounded-full border border-border-strong bg-surface px-3 py-2 text-[12px] text-foreground transition hover:border-mint/40 hover:bg-mint-soft hover:text-mint-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50';
const SUGGESTION_ICON_CLASS = 'size-3.5 text-muted group-hover:text-mint-ink';

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

  return (
    // Desktop centres the card and the input column as one group. Mobile
    // DOESN'T get the tall centred container: it fights the iOS keyboard
    // (focusing the composer leaves a big gap / pushes it off-screen).
    // Top-aligned normal flow lets iOS scroll the focused composer into view
    // above the keyboard the way it does for any in-flow input, and the shell
    // already keeps the tab bar clear of the bottom of this page.
    <div className="flex w-full flex-col items-center gap-5 md:min-h-[calc(100dvh-7rem)] md:justify-center">
      {showReadyBanner && currentBackend && (
        <div className="w-full max-w-[640px]">
          <ReadyBanner backend={currentBackend} onDismiss={dismissReadyBanner} />
        </div>
      )}

      {/* Welcome card — a centred bounded panel, not a full-bleed fill. */}
      <div className="flex w-full max-w-[640px] flex-col items-center gap-6 rounded-2xl border border-border bg-surface-2 px-6 py-10">
        <div className="flex size-14 items-center justify-center rounded-2xl border border-mint/40 bg-mint-soft text-mint-ink shadow-glow-md-mint">
          <Sparkles className="size-6" />
        </div>
        <div className="flex max-w-[520px] flex-col items-center gap-3 text-center">
          <h1 className="text-[22px] font-semibold text-foreground">{t('workbench.home.heroTitle')}</h1>
          <p className="text-[13px] leading-[1.55] text-muted">{t('workbench.home.heroBody')}</p>
        </div>
        {/* Three first moves. Each one keeps the owner this home already has:
            the directory browser, the Agents route and the background-task
            dialog — the pills restore the look, not an older destination. */}
        <div className="flex flex-wrap items-center justify-center gap-2">
          {canCreateProject && (
            <button
              type="button"
              onClick={() => setNewProjectOpen(true)}
              disabled={ns.sending}
              className={SUGGESTION_PILL_CLASS}
            >
              <FolderPlus className={SUGGESTION_ICON_CLASS} />
              <span>{t('workbench.home.openProject')}</span>
            </button>
          )}
          {capabilities.can_manage_agents && (
            // A route, so it still opens in a new tab on middle-click.
            <Link to="/agents" className={SUGGESTION_PILL_CLASS}>
              <Bot className={SUGGESTION_ICON_CLASS} />
              <span>{t('workbench.home.manageAgents')}</span>
            </Link>
          )}
          <button
            type="button"
            onClick={() => setNewTaskOpen(true)}
            disabled={ns.sending}
            className={SUGGESTION_PILL_CLASS}
          >
            <Activity className={SUGGESTION_ICON_CLASS} />
            <span>{t('workbench.home.createTask')}</span>
          </button>
        </div>
      </div>

      {/* Input — the project chips and the Agent picker stand above the shared
          Composer, so where the session lands and which Agent runs it are both
          visible before anything is typed. */}
      <div className="flex w-full max-w-[640px] flex-col gap-3">
        {ns.projects.length > 0 && (
          <ProjectPicker
            projects={ns.projects}
            targetId={ns.target?.id}
            onSelect={ns.setSelected}
            onNewProject={() => setNewProjectOpen(true)}
            disabled={ns.sending}
          />
        )}
        <div className="flex min-w-0 flex-col gap-2">
          <div className="font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t('newSession.agent')}</div>
          <AgentRoutePicker
            value={ns.agentRoute}
            agents={ns.agents}
            onChange={ns.setAgentRoute}
            defaultLabel={ns.effectiveDefaultAgentName
              ? t('newSession.defaultAgentNamed', { name: ns.effectiveDefaultAgentName })
              : t('newSession.defaultAgent')}
            disabled={ns.sending}
            align="start"
            // Fill the column on mobile (≈ full screen); on desktop hug content
            // and cap at 62% so it reads like the compact Chat-header picker
            // instead of a full-width 640px bar. self-start defeats the flex-col
            // stretch that would otherwise force the trigger to the column width.
            triggerClassName="max-w-full sm:max-w-[62%] sm:self-start"
          />
        </div>
        <Composer
          stageMedia
          onSend={send}
          placeholder={t('workbench.home.inputPlaceholder')}
          disabled={ns.sending || !ns.loaded}
          sendDisabled={Boolean(ns.uncertainSessionId)}
          className="max-w-[640px]"
        />
        {ns.needsProject && (
          <div className="px-2 text-[10.5px] text-gold-ink">{t('workbench.home.noProjectForChat')}</div>
        )}
        {ns.error && (
          <div className="mt-1 rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
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
