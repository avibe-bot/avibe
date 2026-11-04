import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';

import { useBots } from '../../context/BotsContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { selectApiErrorFields } from '../../context/apiErrorParse';
import { apiFetch } from '../../lib/apiFetch';
import { MAIN_BOT_SESSION_ID, botSessionPath } from '../../lib/bots';
import { sortProjectsByRecent } from '../../lib/projectOrder';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { settingsOverlayContinuationState } from '../../lib/settingsOverlay';
import { MODEL_HUB_SETTINGS_PATH } from '../settings/models/modelHubRoutes';
import { Button } from '../ui/button';
import { AgentRoutePicker } from '../workbench/AgentRoutePicker';
import { Composer, type ComposerAttachment, type ComposerHandle } from '../workbench/Composer';
import { HomeAsks } from './HomeAsks';
import { useHomeAsks } from './homeCopy';
import { HomeGreeting } from './HomeGreeting';
import { HomeGuide } from './HomeGuide';
import { HomeLayout } from './HomeLayout';
import { useMainBotRoute, useNoModel } from './useMainBot';

const MAIN_BOT_PATH = botSessionPath({ session_id: MAIN_BOT_SESSION_ID });

/**
 * The Owner's home (ui/design: home/home/a-new-desktop … c-ready-phone): the group whose last block
 * is a composer addressed to the main Bot, with no project to pick. Sending is a message in the main
 * Bot's conversation, and the home opens that conversation once the message is in. Its route is the
 * one the conversation's own picker shows: the Agent is the Bot's, so only the model and effort move.
 *
 * With no model one thing leads: the greeting introduces the Bot, the suggestions go quiet, and the
 * composer is disabled, offering "Add a model" where its route goes (Settings · Models).
 */
export function MainBotHome() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const surfaceActive = useRouteSurfaceActive();
  const { capabilities } = useInstanceAuthorization();
  const { mainBot, displayName } = useBots();
  const { projects } = useWorkbenchProjectsTree();
  const composerRef = useRef<ComposerHandle>(null);
  const route = useMainBotRoute(true);
  // The main Bot's own Agent decides which backend has to supply its model.
  const backend = route.agent?.backend ?? route.session?.agent_backend ?? null;
  const noModel = useNoModel(backend, capabilities.can_manage_instance);
  const name = mainBot ? displayName(mainBot) : t('bots.mainDefaultName');
  const recentProject = projects ? sortProjectsByRecent(projects)[0]?.display_name ?? null : null;
  const asks = useHomeAsks({ member: false, project: recentProject });

  const sendingRef = useRef(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A send whose outcome is unknown may already be in the conversation: the draft stays, and
  // sending it again waits until the user has looked.
  const [uncertain, setUncertain] = useState(false);
  // Opening the conversation waits while Settings covers the home, as any navigation from it does.
  const [sent, setSent] = useState(false);
  useEffect(() => {
    if (surfaceActive && sent) navigate(MAIN_BOT_PATH);
  }, [navigate, sent, surfaceActive]);

  const send = async (
    text: string,
    attachments: ComposerAttachment[] = [],
    _references?: unknown,
    metadata?: Record<string, unknown>,
  ): Promise<boolean> => {
    const ready = attachments.filter((attachment) => attachment.status === 'ready');
    if ((!text.trim() && ready.length === 0) || sendingRef.current) return false;
    sendingRef.current = true;
    setSending(true);
    setError(null);
    try {
      const content = ready.length > 0
        ? {
            text,
            attachments: ready.map(({ token, name: fileName, mime, size, kind, url, width, height }) => ({
              token, name: fileName, mime, size, kind, url, width, height,
            })),
          }
        : undefined;
      let response: Response;
      try {
        response = await apiFetch(`/api/sessions/${encodeURIComponent(MAIN_BOT_SESSION_ID)}/messages`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, ...(content ? { content } : {}), ...(metadata ? { metadata } : {}) }),
        });
      } catch {
        setUncertain(true);
        setError(t('bots.conversation.sendStatusUnknown', { name }));
        return false;
      }
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        // Only a proven refusal frees the draft for another try; a timeout after the controller
        // took the message (`dispatch_pending`), a 5xx, or anything else may already be in the
        // conversation, so it is treated like a lost response (the same rule as useNewSession).
        const refused = body?.state === 'retired'
          || ([400, 403, 404, 409, 422].includes(response.status) && body?.dispatch_error !== 'dispatch_pending');
        if (!refused) {
          setUncertain(true);
          setError(t('bots.conversation.sendStatusUnknown', { name }));
          return false;
        }
        const parsed = body ? selectApiErrorFields(body, `HTTP ${response.status}`) : null;
        setError(
          parsed
            ? parsed.code ? t(`errors.${parsed.code}`, { defaultValue: parsed.fallback }) : parsed.fallback
            : `HTTP ${response.status}`,
        );
        return false;
      }
      setSent(true);
      return true;
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };

  const addModel = () => navigate(MODEL_HUB_SETTINGS_PATH, { state: settingsOverlayContinuationState(location) });
  const session = route.session;
  return (
    <HomeLayout>
      <HomeGreeting bot={mainBot} name={name} introduce={noModel} />
      <HomeGuide owner noModel={noModel} name={name} onAsk={(ask) => composerRef.current?.setDraft(ask)} />
      <HomeAsks asks={asks} quiet={noModel} onAsk={(ask) => composerRef.current?.setDraft(ask)} />
      <div className="flex flex-col gap-2">
        <Composer
          ref={composerRef}
          layout="stacked"
          sessionId={MAIN_BOT_SESSION_ID}
          onSend={send}
          disabled={noModel || sending}
          sendDisabled={uncertain}
          placeholder={noModel ? t('workbench.home.noModelPlaceholder') : t('workbench.home.placeholder', { name })}
          holdPlaceholder={noModel ? undefined : t('voice.composer.botPlaceholder', { name })}
          voiceTarget={{ name, avatar: mainBot?.avatar ?? null }}
          actions={noModel ? (
            <Button type="button" variant="text" size="compact" onClick={addModel}>
              {t('workbench.home.addModel')}
              <ChevronRight />
            </Button>
          ) : session && (
            <AgentRoutePicker
              presentation="compact"
              agentLocked
              value={session}
              agents={route.agent ? [route.agent] : []}
              onChange={route.change}
              saving={route.saving}
              allowedBackends={session.agent_backend ? [session.agent_backend] : undefined}
              align="end"
              triggerClassName="max-w-[min(60vw,360px)]"
              speed={session.speed ?? null}
              onSpeedChange={(speed) => route.change({ speed })}
            />
          )}
        />
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
            {error}
            {uncertain && (
              <Link className="ml-2 underline underline-offset-2" to={MAIN_BOT_PATH}>
                {t('newSession.inspectSession')}
              </Link>
            )}
          </div>
        )}
      </div>
    </HomeLayout>
  );
}
