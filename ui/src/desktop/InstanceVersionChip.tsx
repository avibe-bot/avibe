// The version chip while the desktop app shows an instance on another computer (ui/design:
// update/instance-and-app): the instance's own version, beside its name. The instance's page reads it
// from its own `/api/version`; the chip only asks the shell to present the update panel, which the shell
// draws with a section for the instance and one for this computer's desktop app.
import { useTranslation } from 'react-i18next';
import { RefreshCw } from 'lucide-react';
import { VersionChip } from '../components/VersionChip';
import { useFollowedVersion } from '../components/update/useFollowedVersion';
import { normalizeVersion } from '../components/update/versions';

export function InstanceVersionChipView({ name, version, attention = false, updating = false, onClick }: {
  name: string;
  /** None until the instance's version has been read. */
  version: string | null;
  /** The instance or this computer's desktop app can be updated. */
  attention?: boolean;
  /** The instance is installing an update or restarting into it. */
  updating?: boolean;
  onClick?: () => void;
}) {
  const { t } = useTranslation();
  if (!version) return null;
  const shown = `${t('dashboard.versionPrefix')}${version}`;
  const label = t('desktopClient.instanceVersion', { name, version: shown });
  return (
    <VersionChip attention={attention && !updating} updateDot={attention && !updating} title={label} aria-label={label} onClick={onClick}>
      {updating && <RefreshCw size={12} aria-hidden="true" className="animate-spin" />}
      {shown}
    </VersionChip>
  );
}

export function InstanceVersionChip({ name, appUpdate, onPresent }: {
  name: string;
  /** This computer's desktop app has an update, from the shell's projection. */
  appUpdate: boolean;
  onPresent: () => void;
}) {
  const { info, updating, watch } = useFollowedVersion();
  // A desktop-managed host is known by its desktop app's version, as its panel names it.
  const version = info ? normalizeVersion(info.desktop_version ?? info.current) : null;
  return (
    <InstanceVersionChipView
      name={name}
      version={version}
      attention={appUpdate || info?.has_update === true}
      updating={updating}
      onClick={() => { watch(); onPresent(); }}
    />
  );
}
