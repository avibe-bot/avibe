import { useEffect, type FC } from 'react';

import { apiFetch } from '@/lib/apiFetch';
import { reportNativeDevice } from '@/lib/deviceReport';
import { hasNativeCapability } from '@/lib/native';

/** In the iOS App, tells this instance's device list who this iPhone is. */
export const NativeDeviceReporter: FC = () => {
  useEffect(() => {
    if (hasNativeCapability('device')) void reportNativeDevice(apiFetch);
  }, []);
  return null;
};
