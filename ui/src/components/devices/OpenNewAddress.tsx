// Remote access is on, and this browser stopped being this computer the moment it was (ui/design:
// settings/devices/moved*). Drawn by the Devices page while it is open, and by the entry guard when
// the page is opened again. Kept small: the entry loads it.
import { Check, Copy, ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { KeptTail } from '../ui/kept-tail';
import { AVIBE_BOT_APP_URL, instanceUrl } from './paths';

/**
 * Remote access is on, and this page can no longer read anything: it was opened at a private-network
 * address, and after binding that address needs a sign-in. With the new address it gives the link;
 * without (the page could not read it before it lost access) it points to avibe.bot, where the
 * person's Avibe is listed. The address is never taken from the address bar.
 */
export function OpenNewAddress({ host, onCopy }: { host?: string; onCopy?: (url: string) => void }) {
  const { t } = useTranslation();
  const url = host ? instanceUrl(host) : AVIBE_BOT_APP_URL;
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-edge bg-surface px-5 py-4 text-left shadow-elevation-0">
      <div className="flex items-center gap-2">
        <Badge variant="success"><Check className="size-3" />{t('devices.newAddress.on')}</Badge>
        <span className="text-[14px] font-semibold text-foreground">{t('devices.newAddress.title')}</span>
      </div>
      <p className="text-[13px] leading-relaxed text-foreground">
        <KeptTail text={t(host ? 'devices.newAddress.body' : 'devices.newAddress.bodyUnknown')} />
      </p>
      {host && (
        <div className="flex flex-wrap items-center gap-2">
          <a href={url} className="break-all font-mono text-[14px] font-semibold text-cyan-ink hover:underline">{url}</a>
          <Button variant="ghost" size="2xs" aria-label={t('devices.reach.copyAddress')} onClick={() => onCopy?.(url)}><Copy /></Button>
        </div>
      )}
      <div>
        <Button asChild variant="brand">
          <a href={url}><ExternalLink />{t(host ? 'devices.newAddress.open' : 'devices.newAddress.openAvibeBot')}</a>
        </Button>
      </div>
      <p className="text-[11px] leading-relaxed text-muted"><KeptTail text={t('devices.newAddress.note')} /></p>
    </div>
  );
}
