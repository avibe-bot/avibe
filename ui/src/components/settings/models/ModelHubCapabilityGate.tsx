import * as React from 'react';
import { Navigate } from 'react-router-dom';

import { useModelHubCapability } from './useModelHubCapability';
import { MODEL_HUB_DISABLED_REDIRECT, MODEL_HUB_SETTINGS_PATH, modelHubRouteTarget } from './modelHubRoutes';

export const ModelHubRenderBoundary: React.FC<{
  enabled: boolean | null;
  disabled?: React.ReactNode;
  children: React.ReactNode;
}> = ({ enabled, disabled = null, children }) => {
  if (enabled === null) return null;
  return enabled ? children : disabled;
};

export const ModelHubCapabilityGate: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const enabled = useModelHubCapability();
  return (
    <ModelHubRenderBoundary
      enabled={enabled}
      disabled={<Navigate to={MODEL_HUB_DISABLED_REDIRECT} replace />}
    >
      {children}
    </ModelHubRenderBoundary>
  );
};

export const LegacyModelHubRoute: React.FC = () => {
  const enabled = useModelHubCapability();
  if (enabled === null) return null;
  return (
    <Navigate
      to={modelHubRouteTarget(MODEL_HUB_SETTINGS_PATH, enabled)}
      replace
    />
  );
};
