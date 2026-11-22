// The Cloudflare tunnel's own controls, one component each, so Settings · Remote access and the
// Devices 「连接方式」 page (ui/design: settings/devices-connection/*) draw the same control with
// the same options and copy. Props only; each caller owns where the value comes from and when it
// is applied.
import { useTranslation } from 'react-i18next';

import type { RemoteAccessSettings, TunnelNetworkInterface } from '@/context/ApiContext';
import { SegmentedRadio } from '../ui/segmented';
import { Select } from '../ui/select';
import { Switch } from '../ui/switch';

type ControlProps<T> = { value: T; onChange: (next: T) => void; disabled?: boolean };

export function TunnelProtocolControl({ value, onChange, disabled }: ControlProps<RemoteAccessSettings['transport_protocol']>) {
  const { t } = useTranslation();
  return (
    <SegmentedRadio
      value={value}
      onChange={onChange}
      ariaLabel={t('remoteAccess.protocol')}
      disabled={disabled}
      tone="cyan"
      options={[
        { id: 'auto', label: t('remoteAccess.protocolAuto') },
        { id: 'quic', label: 'QUIC' },
        { id: 'http2', label: 'HTTP/2' },
      ]}
    />
  );
}

export function TunnelAutoRecoverySwitch({ value, onChange, disabled }: ControlProps<boolean>) {
  const { t } = useTranslation();
  return (
    <Switch
      size="sm"
      checked={value}
      onCheckedChange={onChange}
      label={t('remoteAccess.autoRecovery')}
      disabled={disabled}
    />
  );
}

export function TunnelProfileControl({ value, onChange, disabled }: ControlProps<RemoteAccessSettings['optimization_profile']>) {
  const { t } = useTranslation();
  return (
    <SegmentedRadio
      value={value}
      onChange={onChange}
      ariaLabel={t('remoteAccess.optimizationProfile')}
      disabled={disabled}
      options={[
        { id: 'stable', label: t('remoteAccess.profileStable') },
        { id: 'balanced', label: t('remoteAccess.profileBalanced') },
        { id: 'low_latency', label: t('remoteAccess.profileLowLatency') },
      ]}
    />
  );
}

export function TunnelIpFamilyControl({ value, onChange, disabled }: ControlProps<RemoteAccessSettings['edge_ip_version']>) {
  const { t } = useTranslation();
  return (
    <SegmentedRadio
      value={value}
      onChange={onChange}
      ariaLabel={t('remoteAccess.ipFamily')}
      disabled={disabled}
      options={[
        { id: '4', label: t('remoteAccess.ipV4') },
        { id: 'auto', label: t('remoteAccess.ipAuto') },
        { id: '6', label: t('remoteAccess.ipV6') },
      ]}
    />
  );
}

/** The outbound interface: the system's choice, or one of this computer's addresses. */
export function TunnelOutboundSelect({ value, onChange, disabled, interfaces, className }: ControlProps<string> & {
  interfaces: TunnelNetworkInterface[];
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <Select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      disabled={disabled}
      aria-label={t('remoteAccess.outboundInterface')}
      className="font-mono text-[12px]"
      wrapperClassName={className}
    >
      <option value="">{t('remoteAccess.outboundSystem')}</option>
      {interfaces.map((networkInterface) => (
        <option key={networkInterface.id} value={networkInterface.address}>
          {t('remoteAccess.interfaceOption', { name: networkInterface.name, address: networkInterface.address })}
        </option>
      ))}
    </Select>
  );
}
