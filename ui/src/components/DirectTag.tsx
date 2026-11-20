import { useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';

import { liveStreamVia, subscribeLiveStreamVia } from '../lib/liveStreamVia';
import { nativeDirectPathKind, nativeDirectPathReported, onNativeDirectPreference } from '../lib/native';
import { directTagKind } from '../lib/remoteAccessDirect';

const unknown = () => null;

// The App's path, as its latest status said (`direct` version 3); null in a browser.
const subscribeAppPath = (onChange: () => void) => onNativeDirectPreference(() => onChange());

// A small tag beside the instance name while this page's live stream runs through the instance's
// direct ingress, as that stream itself reported, naming the App's kind of path: 「直连」 only for
// an IPv4 or IPv6 address, 「中继」 through the relay. A relayed path is never called direct. Any
// other way in, a path the App does not name, or an App that does not keep its path current (before
// `direct` version 3), shows nothing; it counts nothing
// (ui/design: mobile-app/direct-tag).
export const DirectTag: React.FC = () => {
  const { t } = useTranslation();
  const via = useSyncExternalStore(subscribeLiveStreamVia, liveStreamVia, unknown);
  const appPath = useSyncExternalStore(subscribeAppPath, nativeDirectPathKind, unknown);
  const kind = directTagKind(via, appPath, nativeDirectPathReported());
  if (kind === 'direct') {
    return (
      <span className="shrink-0 rounded-full border border-mint/30 bg-mint-soft px-1.5 text-2xs font-semibold text-mint-ink">
        {t('appShell.directTag')}
      </span>
    );
  }
  if (kind === 'relay') {
    return (
      <span className="shrink-0 rounded-full border border-border bg-surface px-1.5 text-2xs font-semibold text-muted">
        {t('appShell.relayTag')}
      </span>
    );
  }
  return null;
};
