import * as React from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { isChromelessShellPath, settingsOverlayOriginFromState } from '@/lib/settingsOverlay';
import { useModelHubCapability } from './useModelHubCapability';
import { modelHubDisabledRedirect } from './modelHubRoutes';

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
  const location = useLocation();
  const overSetup = isChromelessShellPath(
    settingsOverlayOriginFromState(location.state)?.location.pathname ?? '',
  );
  return (
    <ModelHubRenderBoundary
      enabled={enabled}
      disabled={<Navigate to={modelHubDisabledRedirect(overSetup)} replace state={location.state} />}
    >
      {children}
    </ModelHubRenderBoundary>
  );
};
