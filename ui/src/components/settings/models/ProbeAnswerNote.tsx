// What a single check says where it was asked (a Tools row, a Source chip) when
// its answer is not plain evidence: why a request failed, that the model did not
// use the tool, or that the replay after a verified call did not verify.
import { useTranslation } from 'react-i18next';
import { AlertCircle } from 'lucide-react';

import { probeUnanswered } from './hostedTools';
import { serverText } from './serverCopy';
import type { HostedToolProbeResult } from './types';

/**
 * What a probe that became no evidence says where it was asked (a Tools row, a
 * Source chip): a request that failed says why, and one that completed without
 * the tool says that. Nothing for an answer that is evidence.
 */
export const ProbeAnswerNote: React.FC<{ result: HostedToolProbeResult | null }> = ({ result }) => {
  const { t } = useTranslation();
  if (!probeUnanswered(result)) return result ? <ReplayNote replay={result.replay} /> : null;
  const failure = serverText(t, result?.call.error);
  return (
    <span className="model-hub-tool-cost-note flex items-center gap-1 model-hub-ink-gold" role="status">
      <AlertCircle className="size-3 shrink-0" aria-hidden />
      {failure
        ? t('settings.models.tools.probe.outcome.transientReason', { reason: failure })
        : result?.call.outcome === 'inconclusive'
          ? t('settings.models.tools.probe.outcome.inconclusive')
          : t('settings.models.tools.probe.outcome.transient')}
    </span>
  );
};

/**
 * The replay step a verified call is followed by, when it ran and did not verify.
 * Null (no replay ran) and a verified replay say nothing; a replay that got no
 * answer asks for another try, never reads as a failure.
 */
const ReplayNote: React.FC<{ replay: HostedToolProbeResult['replay'] }> = ({ replay }) => {
  const { t } = useTranslation();
  if (!replay || replay.outcome === 'verified') return null;
  return (
    <span className="model-hub-tool-cost-note flex items-center gap-1 model-hub-ink-gold" role="status">
      <AlertCircle className="size-3 shrink-0" aria-hidden />
      {t(replay.outcome === 'unsupported' ? 'settings.models.tools.probe.replay.unsupported' : 'settings.models.tools.probe.replay.unverified')}
    </span>
  );
};
