import { useRef, useState } from 'react';
import { ArrowRight, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../context/ApiContext';
import { Button } from '../ui/button';
import { CollaborationStory } from '../onboarding/CollaborationStory';
import { AccessTiles } from '../onboarding/AccessTiles';
import { ASSISTANT_ORDER } from '../onboarding/collaborationTimeline';
import { DEFAULT_AGENT_STATE } from '@/lib/agentBackends';
import '../onboarding/onboarding.css';

interface WelcomeProps {
  data?: { agents?: Record<string, { cli_path?: string }> };
  onNext: (data: unknown) => void | Promise<void>;
}

export function Welcome({ data, onNext }: WelcomeProps) {
  const { t } = useTranslation();
  const api = useApi();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const start = async () => {
    if (busy.current) return;
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
  return (
    <div className="onboarding-welcome">
      <header className="onboarding-heading">
        <h1>{t('onboarding.welcome.title')}</h1>
        <p>{t('onboarding.welcome.subtitle')}</p>
      </header>
      {/* The stage both steps share, so this button and the setup step's land on the
          same coordinates — see `.onboarding-stage` in onboarding.css. */}
      <div className="onboarding-stage">
        <CollaborationStory />
        <AccessTiles />
      </div>
      <Button type="button" variant="brand" className="group onboarding-action-w onboarding-primary-action" onClick={() => void start()} disabled={pending}>
        {t(pending ? 'onboarding.welcome.detecting' : error ? 'common.retry' : 'onboarding.welcome.getStarted')}
        {pending ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : <ArrowRight size={16} className="motion-safe:transition-transform motion-safe:duration-180 motion-safe:group-hover:translate-x-1" />}
      </Button>
      {error && <div role="alert" className="text-center text-sm text-destructive-ink">
        <p>{t('onboarding.welcome.detectionFailed')}</p>
        <details className="mt-2 max-w-xl break-words"><summary>{t('onboarding.details')}</summary>{error}</details>
      </div>}
    </div>
  );
}
