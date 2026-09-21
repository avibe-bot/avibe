// The first two cards of the diagram, and the third that offers more.
//
// A card is either a fact or an offer, and the difference is what it does when
// pressed: a connected card does nothing, a detected card toggles consent, an empty
// card opens the add dialog. Only the detected one carries `aria-pressed`, because
// only it is a toggle — giving a connected card a pressed state would announce that
// something already true can be turned off here, which it cannot.
import type { FC } from 'react';
import { Check, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { VendorGlyph } from '@/components/settings/models/vendorGlyph';

import type { ProviderSlot } from './providerStage';

export const ProviderCard: FC<{
  slot: ProviderSlot;
  selected: boolean;
  onToggle: () => void;
  onAdd: () => void;
}> = ({ slot, selected, onToggle, onAdd }) => {
  const { t } = useTranslation();
  const detected = slot.kind === 'detected';
  const connected = slot.kind === 'connected';

  // The second line says where the card's knowledge came from. An empty card has no
  // key to describe, so it describes the offer instead.
  const keyLine = slot.mask
    ? t(connected ? 'onboarding.providers.cardKeyAdded' : 'onboarding.providers.cardKeyDetected', { mask: slot.mask })
    : t('onboarding.providers.cardAddKeyNamed', { name: slot.label });

  const label = connected
    ? t('onboarding.providers.cardUseExistingNamed', { name: slot.label })
    : detected
      ? t('onboarding.providers.cardSelected')
      : t('onboarding.providers.cardAddSubscriptionNamed', { name: slot.label });

  return (
    <button
      type="button"
      className="setup-provider-card"
      data-provider={slot.vendor}
      data-state={slot.kind}
      {...(detected ? { 'aria-pressed': selected } : {})}
      aria-label={detected ? `${slot.label} · ${keyLine}` : label}
      disabled={connected}
      onClick={detected ? onToggle : onAdd}
    >
      {/* The catalog's own mark, filed by the same vendor id the slot carries — the
          picker, the import rows and this card therefore draw one provider one way,
          and a vendor with no published mark falls back to its initial rather than
          to a neighbour's logo. */}
      <span className="setup-provider-logo">
        <VendorGlyph vendor={slot.vendor} />
      </span>
      <span className="setup-provider-copy">
        <span className="setup-provider-name">{slot.label}</span>
        <span className="setup-provider-key">{keyLine}</span>
      </span>
      {/* The check states a decision, not a label: the reference draws no word
          beside it, and the card's accessible name already carries the meaning. */}
      {(connected || selected) && (
        <Check className="setup-provider-check" strokeWidth={2.2} aria-hidden="true" />
      )}
    </button>
  );
};

/** The third slot. Always present, whatever the first two hold. */
export const AddMoreCard: FC<{ count: number; onAdd: () => void }> = ({ count, onAdd }) => {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      className="setup-provider-card"
      data-state="add"
      aria-label={t('onboarding.providers.addMoreAction')}
      onClick={onAdd}
    >
      <Plus className="setup-provider-add-icon" strokeWidth={1.6} aria-hidden="true" />
      <span className="setup-provider-copy">
        <span className="setup-provider-name">{t('onboarding.providers.addMoreTitle')}</span>
        <span className="setup-provider-key">{t('onboarding.providers.addMoreSubtitle')}</span>
      </span>
      {count > 0 && <span className="setup-provider-badge">{t('onboarding.providers.addMoreCount', { count })}</span>}
    </button>
  );
};
