import { useEffect, type FC } from 'react';

import { apiFetch } from '@/lib/apiFetch';
import { reportNativeDevice } from '@/lib/deviceReport';
import { DESKTOP_DEVICE_EVENT, desktopDeviceDescription, hasNativeCapability } from '@/lib/native';
import { onPageReactivated } from '@/lib/pageActivity';

/** In the iOS App or the Desktop app, tells this instance's device list who this iPhone or computer is. */
export const NativeDeviceReporter: FC = () => {
  useEffect(() => {
    const report = () => {
      if (!hasNativeCapability('device') && desktopDeviceDescription() === undefined) return;
      void reportNativeDevice(apiFetch);
    };
    report();
    // The Desktop app describes the computer once this page is its committed view of the
    // instance, which may be after the page has mounted.
    window.addEventListener(DESKTOP_DEVICE_EVENT, report);
    // Once a page has reported, this does nothing; it retries one the App
    // refused while it was still confirming the account.
    const stop = onPageReactivated(report);
    return () => {
      window.removeEventListener(DESKTOP_DEVICE_EVENT, report);
      stop();
    };
  }, []);
  return null;
};
