import { useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { nativeDirectPath, subscribeNativeDirectPath } from '../lib/native';

const noPath = () => null;

// One quiet line under the instance name in the phone app: which path this page's new
// connections try first, from the shell's `direct.status()` and `direct.transportChanged`
// (docs/plans/remote-direct-ios.md section 10). Never where an existing connection runs.
// Without the `direct` capability it renders nothing and reads nothing
// (ui/design: mobile-app/direct-path).
export const DirectPathLine: React.FC<{ className?: string }> = ({ className }) => {
  const { t } = useTranslation();
  const path = useSyncExternalStore(subscribeNativeDirectPath, nativeDirectPath, noPath);
  if (!path) return null;
  const route = t(path.preferred === 'direct' ? 'appShell.directPath.direct' : 'appShell.directPath.tunnel');
  const line = t('appShell.directPath.preferred', { route });
  return (
    <p className={cn('truncate text-xs leading-3.5 text-muted', className)} data-testid="direct-path-line">
      {path.closure > 0
        ? t('appShell.directPath.withClosures', {
          line,
          closures: t('appShell.directPath.closures', { count: path.closure }),
        })
        : line}
    </p>
  );
};
