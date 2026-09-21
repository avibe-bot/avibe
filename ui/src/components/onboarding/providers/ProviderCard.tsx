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

import type { SourceKind } from '@/components/settings/models/types';
import { VendorGlyph } from '@/components/settings/models/vendorGlyph';
import type { TranslationKey } from '@/i18n/types';

import type { ProviderSlot } from './providerStage';

/** What a connected card says when there is no mask to say — Settings' own words for
 *  the same distinction, so one source reads one way in both places. */
const SUPPLY_KIND_COPY = {
  subscription: 'settings.models.upstream.kind.subscription',
  api_key: 'settings.models.upstream.kind.apiKey',
} as const satisfies Record<SourceKind, TranslationKey>;

export const ProviderCard: FC<{
  slot: ProviderSlot;
  selected: boolean;
  onToggle: () => void;
  onAdd: () => void;
}> = ({ slot, selected, onToggle, onAdd }) => {
  const { t } = useTranslation();
  const detected = slot.kind === 'detected';
  const connected = slot.kind === 'connected';

  // The second line says where the card's knowledge came from. A connected
  // subscription has no key to mask, so it names the sign-in instead; only a card
  // with nothing behind it describes the offer, which is the one thing a connected
  // card must never do — it is disabled, so an invitation to add a key there is an
  // invitation to press something that does nothing.
  const written = slot.mask
    ? t(connected ? 'onboarding.providers.cardKeyAdded' : 'onboarding.providers.cardKeyDetected', { mask: slot.mask })
    : slot.supply
      ? [t(SUPPLY_KIND_COPY[slot.supply.kind]), slot.supply.account].filter(Boolean).join(' · ')
      : t('onboarding.providers.cardAddKeyNamed', { name: slot.label });

  // A key saved without verification is `standby` carrying `verification_pending`:
  // stored, but nothing has confirmed it answers. Settings states that difference
  // with the same word, and a card that looked identical either way would report an
  // unchecked key as a provider that is already supplying models. It is said in both
  // places the card is read, because `aria-label` replaces the text rather than
  // adding to it.
  const note = slot.pending ? ` · ${t('settings.models.sourceDetail.status.saved')}` : '';
  const keyLine = `${written}${note}`;

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
      {...(slot.pending ? { 'data-pending': 'true' } : {})}
      {...(detected ? { 'aria-pressed': selected } : {})}
      aria-label={detected ? `${slot.label} · ${keyLine}` : `${label}${note}`}
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
