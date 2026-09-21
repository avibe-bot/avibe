import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type Ref } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../context/ApiContext';
import { CollaborationStory } from '../onboarding/CollaborationStory';
import { ASSISTANT_ORDER } from '../onboarding/collaborationTimeline';
import { DEFAULT_AGENT_STATE } from '@/lib/agentBackends';
import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import type { SetupAction, SetupScreenHandle } from '../onboarding/setupFlow';
import '../onboarding/onboarding.css';

interface WelcomeProps {
  active: boolean;
  ref?: Ref<SetupScreenHandle>;
  onActionChange: (action: SetupAction) => void;
  data?: { agents?: Record<string, { cli_path?: string }> };
  onNext: (data: unknown) => void | Promise<void>;
}

export function Welcome({ data, onNext, active, ref, onActionChange }: WelcomeProps) {
  const { t } = useTranslation();
  const api = useApi();
  const routeSurfaceActive = useRouteSurfaceActive();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  // Same commit as the state it describes; see AgentDetection's publication note.
  useLayoutEffect(() => {
    if (active) onActionChange({ labelKey: pending ? 'onboarding.welcome.detecting' : error ? 'common.retry' : 'onboarding.welcome.getStarted', disabled: pending, busy: pending, icon: pending ? 'spinner' : 'arrow-right' });
  }, [active, error, onActionChange, pending]);
  useImperativeHandle(ref, () => ({ activate: () => { void start(); } }));
  const start = async () => {
    if (!active || busy.current) return;
    busy.current = true;
    setPending(true);
    setError(null);
    try {
      const results = await Promise.all(ASSISTANT_ORDER.map(async (name) => {
        const agent = { ...DEFAULT_AGENT_STATE[name], ...data?.agents?.[name] };
        const result = await api.detectCli(agent.cli_path || name);
        return [name, { ...agent, cli_path: result.path || agent.cli_path, status: result.found ? 'ok' : 'missing' }];
      }));
      await onNext({ agents: Object.fromEntries(results), __onboardingDetected: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      busy.current = false;
      setPending(false);
    }
  };
  // The failure explains the action, so it belongs under it — and in the shell that
  // action is the shared pair this screen no longer draws. Left in the screen, a long
  // diagnostic (or an opened `details`) grows the content above the footer and drags
  // the anchor the two steps share, so the shell's slot after the pair takes it.
  const setupRoot = useRef<HTMLDivElement>(null);
  const [actionAside, setActionAside] = useState<HTMLElement | null>(null);
  useEffect(() => {
    setActionAside(setupRoot.current?.closest('.onboarding-step')?.querySelector<HTMLElement>('[data-setup-action-aside]') ?? null);
  }, [onActionChange]);
  const errorNode = error ? (
    <div role="alert" className="text-center text-sm text-destructive-ink">
      <p>{t('onboarding.welcome.detectionFailed')}</p>
      <details className="mt-2 max-w-xl break-words"><summary>{t('onboarding.details')}</summary>{error}</details>
    </div>
  ) : null;
  return (
    <div className="onboarding-welcome" ref={setupRoot}>
      <header className="onboarding-heading">
        <h1 tabIndex={-1}>{t('onboarding.welcome.title')}</h1>
        <p>{t('onboarding.welcome.subtitle')}</p>
      </header>
      {/* The stage both steps share, so this button and the setup step's land on the
          same coordinates — see `.onboarding-stage` in onboarding.css. */}
      <div className="onboarding-stage">
        <CollaborationStory active={active} />
      </div>
      {/* A portal leaves the screen root, and with it the `inert` the shell puts on a
          screen nobody is reading, so the alert answers to the same activity itself.
          Without a shell slot — the standalone host — it stays where it always was. */}
      {errorNode && (active && routeSurfaceActive && actionAside
        ? createPortal(<div className="onboarding-setup-hint">{errorNode}</div>, actionAside)
        : errorNode)}
    </div>
  );
}
