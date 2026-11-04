import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router-dom';

import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { canCreateLocalProject } from '../../lib/sessionInfo';
import { useNewSession } from '../../lib/useNewSession';
import { AgentRoutePicker } from '../workbench/AgentRoutePicker';
import { Composer, type ComposerAttachment, type ComposerHandle } from '../workbench/Composer';
import { NewProjectDialog } from '../workbench/NewProjectDialog';
import { ProjectPicker } from '../workbench/ProjectPicker';
import { HomeAsks } from './HomeAsks';
import { useHomeAsks } from './homeCopy';
import { HomeGreeting } from './HomeGreeting';
import { HomeGuide } from './HomeGuide';
import { HomeLayout } from './HomeLayout';

/**
 * A member's home (ui/design: home/home/d-member-*): the same group, without Bots. The composer
 * starts a new Session in the chosen project, so the project chips stand right above it (8 apart),
 * one block with it, and its route still names the Agent. The home owns one unsent draft: files and
 * voice stay in the composer until Send binds the selected project and Agent and commits the first
 * message.
 */
export function MemberHome() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const surfaceActive = useRouteSurfaceActive();
  const { capabilities } = useInstanceAuthorization();
  const canCreateProject = canCreateLocalProject(capabilities);
  const composerRef = useRef<ComposerHandle>(null);
  const ns = useNewSession({
    active: capabilities.can_chat,
    loadErrorText: t('newSession.loadError'),
    createFailedText: t('newSession.createFailed'),
    errorText: t,
  });
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const asks = useHomeAsks({ member: true, project: ns.target?.display_name ?? null });
  const [sentSessionId, setSentSessionId] = useState<string | null>(null);
  useEffect(() => {
    if (surfaceActive && sentSessionId) navigate(`/chat/${encodeURIComponent(sentSessionId)}`);
  }, [navigate, sentSessionId, surfaceActive]);

  const send = async (
    text: string,
    attachments: ComposerAttachment[] = [],
    _references?: unknown,
    metadata?: Record<string, unknown>,
  ): Promise<boolean> => {
    const result = await ns.send(text, attachments, metadata);
    if (result) {
      // The first POST has completed. Navigation only opens its transcript;
      // handing initialMessage to ChatPage here would start a duplicate turn.
      setSentSessionId(result.sessionId);
      return true;
    }
    if ((text.trim() || attachments.length) && ns.needsProject && canCreateProject) setNewProjectOpen(true);
    return false;
  };

  return (
    <>
      <HomeLayout>
        <HomeGreeting bot={null} name="" introduce={false} />
        <HomeGuide owner={false} noModel={false} name="" onAsk={(ask) => composerRef.current?.setDraft(ask)} />
        <HomeAsks asks={asks} quiet={false} onAsk={(ask) => composerRef.current?.setDraft(ask)} />
        <div className="flex flex-col gap-2">
          {(ns.projects.length > 0 || canCreateProject) && (
            <ProjectPicker
              projects={ns.projects}
              targetId={ns.target?.id}
              onSelect={ns.setSelected}
              onNewProject={() => setNewProjectOpen(true)}
              disabled={ns.sending}
            />
          )}
          <Composer
            ref={composerRef}
            stageMedia
            layout="stacked"
            actions={
              <AgentRoutePicker
                presentation="compact"
                value={ns.agentRoute}
                agents={ns.agents}
                onChange={ns.setAgentRoute}
                defaultLabel={ns.effectiveDefaultAgentName
                  ? t('newSession.defaultAgentNamed', { name: ns.effectiveDefaultAgentName })
                  : t('newSession.defaultAgent')}
                disabled={ns.sending}
                align="end"
                triggerClassName="max-w-[min(60vw,360px)]"
                speed={ns.speed}
                onSpeedChange={ns.setSpeed}
              />
            }
            onSend={send}
            placeholder={t('workbench.home.inputPlaceholder')}
            holdPlaceholder={t('voice.composer.homePlaceholder')}
            voiceTarget={{
              name: ns.target?.display_name ?? '',
              title: ns.target
                ? t('voice.composer.toNewSession', { project: ns.target.display_name })
                : t('voice.composer.toHere'),
            }}
            disabled={ns.sending || !ns.loaded}
            sendDisabled={Boolean(ns.uncertainSessionId)}
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
        </div>
      </HomeLayout>
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
    </>
  );
}
