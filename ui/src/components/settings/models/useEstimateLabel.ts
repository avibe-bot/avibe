import { useTranslation } from 'react-i18next';

import { formatSentences, formatSmallUsd } from './format';
import type { ProbeCostSummary } from './hostedTools';
import type { HostedCostEstimate } from './types';

/** 「约 $0.02」, 「订阅内含」, or nothing when the estimate is unknown. */
export const useEstimateLabel = () => {
  const { t } = useTranslation();
  return (estimate: HostedCostEstimate | null | undefined): string | null => {
    if (!estimate) return null;
    return estimate.basis === 'included'
      ? t('settings.models.tools.probe.included') as string
      : t('settings.models.tools.probe.about', { usd: formatSmallUsd(estimate.usd) }) as string;
  };
};

/**
 * A set of probes' cost in one phrase: the priced sum, then how many are included
 * or have no price, e.g. 「约 $0.04，另 2 项暂无单价」. Only included reads as
 * included; nothing priced at all reads as unknown. The locale composes it.
 */
export const useCostSummaryLabel = () => {
  const { t, i18n } = useTranslation();
  const estimateLabel = useEstimateLabel();
  return ({ usd, priced, included, unpriced }: ProbeCostSummary): string => {
    if (priced === 0 && included === 0) return t('settings.models.tools.probe.unknown') as string;
    const base = priced > 0
      ? estimateLabel({ basis: 'list', usd }) as string
      : t('settings.models.tools.probe.included') as string;
    const extras = [
      ...(priced > 0 && included > 0 ? [t('settings.models.tools.probe.plusIncluded', { count: included }) as string] : []),
      ...(unpriced > 0 ? [t('settings.models.tools.probe.plusUnpriced', { count: unpriced }) as string] : []),
    ];
    return extras.length === 0 ? base : formatSentences([base, ...extras], i18n.language);
  };
};

