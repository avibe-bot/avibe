import { useTranslation } from 'react-i18next';

import { formatSmallUsd } from './format';
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
