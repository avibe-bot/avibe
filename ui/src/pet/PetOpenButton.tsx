import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ExternalLink } from 'lucide-react';

import { petBridge } from './petBridge';
import { usePetTooltip, type PetTooltipPlacement } from './PetTooltip';

export type { PetTooltipPlacement } from './PetTooltip';

/** The pet's Open in Avibe icon button, with the pet tooltip (`usePetTooltip`). */
export function PetOpenButton({ link, placement, className }: { link: string; placement: PetTooltipPlacement; className?: string }) {
  const { t } = useTranslation();
  const label = t('pet.openInAvibe');
  const { ref, triggerProps, tooltip } = usePetTooltip(label, placement);
  return (
    <>
      <button
        ref={ref}
        type="button"
        className={clsx('pet-mini-open', className)}
        // An empty title stops the native tooltip of an ancestor's `title`
        // (an activity card's) from showing over the pet tooltip.
        title=""
        aria-label={label}
        onClick={() => void petBridge.open(link)}
        {...triggerProps}
      >
        <ExternalLink className="size-3.5" />
      </button>
      {tooltip}
    </>
  );
}
