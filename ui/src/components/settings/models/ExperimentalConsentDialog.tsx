// Consent gate for hub-held subscription login (subscription_hub_experimental,
// spec §7). Copy verbatim from the S2 ToS review §9 (ban-risk points). Shown
// ONLY when the experimental channel is chosen; recorded consent marks the
// source 实验.
import * as React from 'react';
import { TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

export const ExperimentalConsentDialog: React.FC<{
  open: boolean;
  onConsent: () => void;
  onCancel: () => void;
}> = ({ open, onConsent, onCancel }) => {
  const { t } = useTranslation();
  // Three ban-risk points (S2 §9), rendered from the i18n array.
  const points = t('settings.models.consent.points', { returnObjects: true }) as string[];

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <DialogContent className="max-w-[520px] gap-5">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-[17px] font-bold">
            <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-gold/15 text-gold">
              <TriangleAlert className="size-4" />
            </span>
            {t('settings.models.consent.title')}
          </DialogTitle>
          <DialogDescription>{t('settings.models.consent.subtitle')}</DialogDescription>
        </DialogHeader>

        <ul className="flex flex-col gap-3 rounded-lg border border-gold/30 bg-gold/[0.06] px-4 py-3.5">
          {(Array.isArray(points) ? points : []).map((point, i) => (
            <li key={i} className="flex gap-2 text-[13px] leading-relaxed text-foreground">
              <span className="mt-2 size-1.5 shrink-0 rounded-full bg-gold" aria-hidden />
              <span>{point}</span>
            </li>
          ))}
        </ul>

        <DialogFooter className="sm:justify-end">
          <Button variant="outline" size="sm" onClick={onCancel}>
            {t('common.cancel')}
          </Button>
          <Button variant="brand-gold" size="sm" onClick={onConsent}>
            {t('settings.models.consent.confirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
