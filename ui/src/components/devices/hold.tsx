// A pairing request this computer saved and has not settled (#4210), where the person has nothing
// to decide, in place of 「开启远程访问」: one sentence, nothing to press. Shared by Settings ·
// Devices and Setup. Never a key or a way to replace one.
import { LoaderCircle } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';

import { KeptTail } from '../ui/kept-tail';
import { SUPPORT_EMAIL } from './features';
import type { PairingHold } from './types';

/** It cannot be confirmed: the fact, and the address to write to. */
export function SupportLine() {
  return (
    <Trans
      i18nKey="devices.hold.support"
      values={{ email: SUPPORT_EMAIL }}
      components={{ mail: <a href={`mailto:${SUPPORT_EMAIL}`} className="font-medium underline underline-offset-2" /> }}
    />
  );
}

export function PairingHoldRow({ hold }: { hold: PairingHold }) {
  const { t } = useTranslation();
  return (
    <div role="status" className="flex items-start gap-2.5 px-5 py-4 text-[13px] leading-relaxed text-foreground">
      {hold.kind === 'finishing' && <LoaderCircle className="mt-0.5 size-4 shrink-0 text-muted motion-safe:animate-spin" />}
      <p className="min-w-0">
        {hold.kind === 'support' ? <SupportLine /> : <KeptTail text={t(`devices.hold.${hold.kind}`)} />}
      </p>
    </div>
  );
}
