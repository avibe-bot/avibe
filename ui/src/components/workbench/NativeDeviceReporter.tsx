import { useEffect, type FC } from 'react';

import { apiFetch } from '@/lib/apiFetch';
import { reportNativeDevice } from '@/lib/deviceReport';
import { desktopDeviceDescription, hasNativeCapability } from '@/lib/native';
import { onPageReactivated } from '@/lib/pageActivity';

/** In the iOS App or the Desktop app, tells this instance's device list who this iPhone or computer is. */
export const NativeDeviceReporter: FC = () => {
  useEffect(() => {
    if (!hasNativeCapability('device') && desktopDeviceDescription() === undefined) return undefined;
    const report = () => { void reportNativeDevice(apiFetch); };
    report();
    // Once a page has reported, this does nothing; it retries one the App
    // refused while it was still confirming the account.
    return onPageReactivated(report);
  }, []);
  return null;
};
