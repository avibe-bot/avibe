// The middle of the diagram: the thing everything above feeds and everything below
// draws from.
//
// The card is the engine's only status surface on this screen. What the lifecycle
// reports — installing, starting, a classified failure, or running — is said here,
// on the object it is about, rather than in a banner somewhere else on the page. A
// running-but-degraded engine still routes, so it reads as running; saying otherwise
// would push someone to repair something that is working.
import type { FC } from 'react';
import { ExternalLink, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { GatewayAdoptionFailure } from '@/components/settings/models/gatewayAdoption';
import { Button } from '@/components/ui/button';
import type { TranslationKey } from '@/i18n/types';

export type GatewayPhase = 'idle' | 'installing' | 'starting' | 'running' | 'failed' | 'unsupported';

const PHASE_LINE = {
  idle: 'onboarding.providers.gatewayTagline',
  installing: 'onboarding.providers.gatewayInstalling',
  starting: 'onboarding.providers.gatewayStarting',
  running: 'onboarding.providers.gatewayTagline',
  failed: 'onboarding.providers.gatewayNotReady',
  unsupported: 'onboarding.providers.gatewayUnsupported',
} as const satisfies Record<GatewayPhase, TranslationKey>;

const BUSY: ReadonlySet<GatewayPhase> = new Set<GatewayPhase>(['installing', 'starting']);

/** Where 「查看安装指南」 goes. The docs site is the project's own published
 *  troubleshooting surface; a deeper path would be one this screen invented, and a
 *  link that 404s is worse than one that lands a page away from the answer. */
const INSTALL_GUIDE_URL = 'https://docs.avibe.bot';

/**
 * @param failedStep Which step of the lifecycle failed, published as a DOM hook
 *   rather than as copy: the two failure sentences C1 ships do not name a step,
 *   and inventing a third that does would be copy this screen made up. It is what
 *   a bug report and a test can read off the rendered card.
 * @param onRetry Absent means the state has no recovery from here. Both failure
 *   lines ask for one ("Try again", "Recheck availability"), so the card carries
 *   it: the engine is what failed, and an action about it belongs on it rather
 *   than in the footer, which is claimed by whatever the person came here to do.
 */
export const GatewayCard: FC<{
  phase: GatewayPhase;
  failedStep?: GatewayAdoptionFailure['step'] | null;
  onRetry?: (() => void) | null;
}> = ({ phase, failedStep, onRetry }) => {
  const { t } = useTranslation();
  const busy = BUSY.has(phase);
  const bad = phase === 'failed' || phase === 'unsupported';

  return (
    <div
      className="setup-gateway"
      data-state={phase}
      {...(bad && failedStep ? { 'data-failed-step': failedStep } : {})}
      // The whole card is one status: a reader should hear what the gateway is
      // doing, not a name and then, separately, a line that changed.
      role="status"
      aria-live="polite"
    >
      <span className="setup-gateway-name">{t('onboarding.providers.gatewayName')}</span>
      <span className="setup-gateway-tagline" {...(bad ? { 'data-tone': 'error' } : {})}>
        {busy && (
          <RefreshCw
            className="setup-gateway-spinner motion-safe:animate-spin inline-block align-[-2px] me-1.5"
            aria-hidden="true"
          />
        )}
        {t(PHASE_LINE[phase])}
      </span>
      {bad && onRetry && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="setup-gateway-retry"
          onClick={onRetry}
        >
          {t('common.retry')}
        </Button>
      )}
      {/* Rechecking availability is the only thing this screen can do about an
          unsupported host, and on its own it is a button that will keep saying no.
          The guide is the other half of that answer — what would have to change for
          the recheck to come back differently — which is why C1 ships the label. */}
      {phase === 'unsupported' && (
        <Button variant="link" size="sm" className="setup-gateway-help gap-1.5 px-0" asChild>
          <a href={INSTALL_GUIDE_URL} target="_blank" rel="noopener noreferrer">
            {t('onboarding.providers.gatewayEnvironmentHelp')}
            <ExternalLink className="size-[13px]" aria-hidden="true" />
          </a>
        </Button>
      )}
    </div>
  );
};
