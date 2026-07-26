import * as React from 'react';

import { useApi } from '@/context/ApiContext';
import { modelHubEnabledFromConfig } from './featureFlags';

export const useModelHubCapability = (): boolean | null => {
  const { getConfig } = useApi();
  const [enabled, setEnabled] = React.useState<boolean | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    getConfig()
      .then((config) => {
        if (!cancelled) setEnabled(modelHubEnabledFromConfig(config));
      })
      .catch(() => {
        if (!cancelled) setEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, [getConfig]);

  return enabled;
};
