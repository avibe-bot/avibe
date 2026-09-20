import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Welcome } from './steps/Welcome';
import { AgentDetection } from './steps/AgentDetection';
import logoImg from '@/assets/logo.png';
import { LanguageSwitcher } from './LanguageSwitcher';
import { useApi, type VibeAgentBrief } from '../context/ApiContext';
import { useStatus } from '../context/StatusContext';
import { setConfigField } from '../lib/configMutations';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { SetupPlatformRecovery, type SavedPlatformRecovery } from './onboarding/SetupPlatformRecovery';
import { getEnabledPlatforms, getPlatformCatalog, platformHasRunnableConfig } from '../lib/platforms';
import { SetupModelRecovery } from './onboarding/SetupModelRecovery';
import { readOpencodeSetupRoutes } from './onboarding/opencodeSetupRoutes';
import { ASSISTANT_ORDER } from './onboarding/collaborationTimeline';

/**
 * The setup's top bar, drawn as the design draws it at every size: the brand lockup —
 * the logo asset, which already carries its white tile, beside the two-line wordmark —
 * at the window's own gutter, and the shared language switcher wearing its round
 * trigger opposite it.
 */
function SetupHeader() {
  const { t } = useTranslation();
  return (
    <header>
      <div className="onboarding-brand">
        <span className="onboarding-brand-mark"><img src={logoImg} alt="" /></span>
        <span className="onboarding-brand-wordmark">
          <strong>{t('onboarding.brand.name')}</strong>
          <span>{t('onboarding.brand.tagline')}</span>
        </span>
      </div>
      <LanguageSwitcher variant="icon-round" />
    </header>
  );
}

export function Wizard() {
  const api = useApi(); const { t } = useTranslation(); const navigate = useNavigate();
  const { control } = useStatus();
  const { capabilities } = useInstanceAuthorization();
  const [platformRecovery, setPlatformRecovery] = useState<SavedPlatformRecovery | null>(null);
  const [recovery, setRecovery] = useState<VibeAgentBrief | null>(null);
  const [step, setStep] = useState<'welcome' | 'agents'>('welcome');
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState('');
  const completing = useRef(false);
  const load = useCallback(async () => {
    setError('');
    try { setData(await api.getConfig()); }
    catch (cause) { setError(String(cause)); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);
  const complete = async () => {
    if (completing.current) return;
    completing.current = true;
    try {
      const freshConfig = await api.getConfig();
      const enabledPlatforms = getEnabledPlatforms(freshConfig);
      const missing = getPlatformCatalog(freshConfig).find((platform) => enabledPlatforms.includes(platform.id) && !platformHasRunnableConfig(freshConfig, platform.id));
      if (missing) {
        setPlatformRecovery({ config: freshConfig, descriptor: missing });
        return;
      }
      const readCandidates = async () => {
        const results = await Promise.allSettled(ASSISTANT_ORDER.map((name) => api.getBackendConnection(name)));
        return results.flatMap((result) => result.status === 'fulfilled' && result.value.ok ? [result.value] : []);
      };
      let connections = await readCandidates();
      if (!connections.some((connection) => connection.entry_eligible)) throw new Error(t('onboarding.connection.entryFailed'));
      if (!connections.some((connection) => connection.ready)) {
        // Only confirmed stopped state allows this explicit start. Pending or
        // unknown IPC never becomes a restart request from the browser.
        if (!connections.some((connection) => connection.entry_eligible && connection.application === 'stopped')) throw new Error(t('onboarding.connection.applyPending'));
        const started = await control('start');
        if (started?.ok === false) throw new Error(started.message || t('onboarding.connection.entryFailed'));
        connections = await readCandidates();
      }
      const ready = new Set(connections.filter((connection) => connection.ready).map((connection) => connection.backend));
      if (!ready.size) throw new Error(t('onboarding.connection.entryFailed'));
      const agents = await api.listVibeAgents({ cache: false });
      const candidates = agents.agents.filter((agent) => agent.enabled && !agent.archived && ready.has(agent.backend as typeof ASSISTANT_ORDER[number]));
      if (!agents.ok || !candidates.length) throw new Error(t('onboarding.connection.entryFailed'));
      let available = candidates.filter((agent) => agent.backend !== 'opencode');
      const opencode = candidates.filter((agent) => agent.backend === 'opencode');
      if (opencode.length) {
        try {
          const routes = await readOpencodeSetupRoutes(api);
          available = [...available, ...opencode.filter((agent) => routes.accepts(agent.model))];
          if (!available.length && routes.mode === 'direct' && capabilities.can_manage_agents) {
            setPlatformRecovery(null);
            setRecovery(opencode.find((agent) => agent.name === agents.default_agent_name) || opencode[0]);
            return;
          }
        } catch (cause) {
          // An unused OpenCode failure cannot block another usable backend.
          if (!available.length) throw cause;
        }
      }
      if (!available.length) throw new Error(t('onboarding.connection.modelUnavailable'));
      // Preserve a usable selected Agent. First setup may have auto-seeded a
      // default for a missing backend; choose a real enabled Agent in that case.
      if (!available.some((agent) => agent.name === agents.default_agent_name)) {
        const selected = await api.setDefaultVibeAgent(available[0].name);
        if (!selected.ok) throw new Error(t('onboarding.connection.entryFailed'));
      }
      // Persist completion last: failed start/readiness leaves AuthGuard's
      // existing setup gate intact. The locked config API validates existing IM.
      await api.mutateConfig([setConfigField(['setup_completed'], true)]);
      navigate('/', { state: { onboardingCompleted: true } });
    } finally { completing.current = false; }
  };
  if (!data) return <div className="min-h-screen flex flex-col items-center justify-center bg-background text-muted">
    {error ? <><p role="alert">{error}</p><button onClick={() => void load()}>{t('common.retry')}</button></> : t('common.loading')}
  </div>;
  return <div className="onboarding-shell">
    <SetupHeader />
    <main className="onboarding-shell-content">
      {step === 'welcome' ? <Welcome data={data} onNext={(next) => { setData({ ...data, ...Object(next) }); setStep('agents'); window.scrollTo({ top: 0, behavior: 'instant' }); }} />
        : <AgentDetection data={data} completionRecovery={platformRecovery ? <SetupPlatformRecovery key={platformRecovery.descriptor.id} saved={platformRecovery} onRepaired={complete} onCancel={() => setPlatformRecovery(null)} /> : recovery ? <SetupModelRecovery key={recovery.id} agent={recovery} onComplete={complete} onCancel={() => setRecovery(null)} /> : undefined} onNext={complete} onBack={(next) => { setData({ ...data, ...next }); setStep('welcome'); window.scrollTo({ top: 0, behavior: 'instant' }); }} />}
    </main>
  </div>;
}
