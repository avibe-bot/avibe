// The one place setup adds a provider, whichever way it is added.
//
// Three methods, one frame. That is the whole design constraint (C5): the box does
// not resize, move or re-anchor when the method changes, because a person comparing
// 「订阅」 against 「API Key」 switches back and forth, and a frame that jumps under
// them makes the comparison cost something. So the header, the method row and the
// footer are anchored, only the middle scrolls, and the frame's height is fixed at
// the tallest ordinary pane rather than fitted to whichever one is showing.
//
// Only the active pane is rendered. `hidden` would have been enough to keep a
// control out of the tab order, but not enough to stop a hidden pane's effects —
// and the effect here is an authorization flow, where a duplicate is a second
// device code for the same account. Rendering one pane makes that unrepresentable
// rather than merely avoided. What it costs is pane state, so pane state lives
// here: the half-typed key survives a switch away and back because nothing
// unmounted it in the first place.
//
// The dialog's lifetime IS its openness — the caller mounts it to open it. That is
// what makes every initial value below a plain `useState` initializer instead of an
// effect that has to decide whether this render is a new opening; an effect that
// re-ran on a prop change would wipe the draft it exists to preserve.
//
// None of the three methods is implemented here. Detected hands rows back to the
// screen, which owns the selection and opens the existing migration takeover;
// subscription launches `OAuthConnectDialog`, which owns the channel choice, the
// poll, the deadline and cancellation; API Key renders the shared
// `ApiKeySourceForm`. This file is the frame, the method choice, and the one write
// that has no other owner.
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Check, CircleX, LoaderCircle, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { DialogOverlay } from '@/components/ui/dialog';
import { ApiKeySourceForm } from '@/components/settings/models/ApiKeySourceForm';
import {
  apiKeySourceCreate,
  apiKeyWriteSettled,
  draftComplete,
  EMPTY_API_KEY_DRAFT,
  selectVendor,
  sourceClientNonce,
  type ApiKeySourceDraft,
} from '@/components/settings/models/apiKeySourceDraft';
import { apiKeyVendorPreset } from '@/components/settings/models/apiKeyVendors';
import type { CollectionReadAuthority } from '@/components/settings/models/collectionReadAuthority';
import {
  createContinuationSettlement,
  type ContinuationTicket,
} from '@/components/settings/models/mutationSettlement';
import { modelsApi, type Adoption, type SourceCreated } from '@/components/settings/models/modelsApi';
import { OAuthConnectDialog } from '@/components/settings/models/OAuthConnectDialog';
import { reconcileUnknownWrite } from '@/components/settings/models/reconcileUnknownWrite';
import { subscriptionChooser } from '@/components/settings/models/subscriptionOptions';
import type { Source } from '@/components/settings/models/types';
import { VendorGlyph } from '@/components/settings/models/vendorGlyph';
import { providerBrandLabel, providerVendorId } from '@/components/settings/providers/providerIdentity';
import type { TranslationKey } from '@/i18n/types';

import { usableSource, type ProviderSlot } from './providerStage';

/**
 * The subscriptions setup offers.
 *
 * Two, named here rather than derived: `ModelsApi` publishes no vendor-capability
 * list, and C6 ruled out inventing a producer for one. The full set lives in
 * `subscriptionOptions.ts` and Settings is where the rest are connected — this is
 * the shortlist the handoff ships, in the shortlist's own order.
 */
const SUBSCRIPTION_CHOICES: readonly string[] = ['openai', 'anthropic'];

export type AddSourceMethod = 'detected' | 'subscription' | 'apiKey';

const METHOD_LABEL = {
  detected: 'onboarding.providers.addMethodDetected',
  subscription: 'onboarding.providers.addMethodSubscription',
  apiKey: 'onboarding.providers.addMethodApiKey',
} as const satisfies Record<AddSourceMethod, TranslationKey>;

/** Where the API-key write is. The other two methods report through the dialog and
 *  the screen that own them, so neither has a state here. */
type WritePhase =
  | { kind: 'idle' }
  | { kind: 'saving' }
  /** Written; the caller is reading back what it landed (C6). */
  | { kind: 'checking' }
  /** The authorization dialog is up; this frame waits under it. */
  | { kind: 'waitingAuth' }
  /** `settled` separates a server verdict from a write whose outcome is unknown —
   *  and an unknown one may never simply be sent again. */
  | { kind: 'failed'; settled: boolean };

const subscriptionBrand = (vendor: string): string =>
  subscriptionChooser(vendor)?.brand ?? providerBrandLabel(vendor);

const DetectedRow: React.FC<{
  slot: ProviderSlot;
  selected: boolean;
  added: boolean;
  onToggle: () => void;
}> = ({ slot, selected, added, onToggle }) => {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      className="setup-add-row"
      data-provider={slot.vendor}
      data-state={added ? 'added' : 'detected'}
      disabled={added}
      {...(added ? {} : { 'aria-pressed': selected })}
      aria-label={t('onboarding.providers.addDetectedSelectNamed', { name: slot.label })}
      onClick={onToggle}
    >
      <span className="setup-add-row-logo"><VendorGlyph vendor={slot.vendor} /></span>
      <span className="setup-add-row-copy">
        <span className="setup-add-row-name">{slot.label}</span>
        {slot.mask && (
          <span className="setup-add-row-detail">
            {t('onboarding.providers.cardKeyDetected', { mask: slot.mask })}
          </span>
        )}
      </span>
      {added
        ? <span className="setup-add-row-tag">{t('onboarding.providers.addDetectedAdded')}</span>
        : selected && <Check className="setup-add-row-check" strokeWidth={2.2} aria-hidden="true" />}
    </button>
  );
};

export const AddSourceDialog: React.FC<{
  /** Opened from 「Add more」 rather than from a named brand slot. It changes what
   *  the dialog says it is for, and nothing about what it can do. */
  more: boolean;
  /** The brand the opener named, when it named one: a subscription brand opens on
   *  its sign-in, any other opens the key form already set to it. */
  vendor?: string | null;
  /** Detected candidates the stage is not already drawing. Empty means the method
   *  does not exist rather than that it is empty. */
  detected: readonly ProviderSlot[];
  /** Rows the current selection would submit — the number the review names. */
  pendingCount: number;
  sources: Source[];
  /** The generation-controlled source read, used to settle an unknown write. */
  sourceReads: CollectionReadAuthority<Source[]>;
  isSelected: (slot: ProviderSlot) => boolean;
  onToggleDetected: (slot: ProviderSlot) => void;
  /** Hand the current selection to the existing migration takeover. */
  onReviewDetected: () => void;
  /** Resolves once the caller has read back what the write landed. `null` is a
   *  landing with no snapshot to report — re-read everything. */
  onAdded: (created: SourceCreated | null) => Promise<void>;
  onClose: () => void;
}> = ({
  more,
  vendor,
  detected,
  pendingCount,
  sources,
  sourceReads,
  isSelected,
  onToggleDetected,
  onReviewDetected,
  onAdded,
  onClose,
}) => {
  const { t } = useTranslation();
  const methods: AddSourceMethod[] = detected.length > 0
    ? ['detected', 'subscription', 'apiKey']
    : ['subscription', 'apiKey'];
  const namedSubscription = vendor && SUBSCRIPTION_CHOICES.includes(vendor) ? vendor : null;

  const [method, setMethod] = React.useState<AddSourceMethod>(
    namedSubscription ? 'subscription' : vendor ? 'apiKey' : methods[0],
  );
  const [subscriptionVendor, setSubscriptionVendor] = React.useState(
    namedSubscription ?? SUBSCRIPTION_CHOICES[0],
  );
  // Through `selectVendor`, so a named brand arrives with its published endpoint
  // already filled. Setting the field alone would be worse than not naming one:
  // re-selecting the vendor that is already selected is identity, so the
  // prefill this brand publishes would be unreachable from inside the form.
  const [draft, setDraft] = React.useState<ApiKeySourceDraft>(
    () => (vendor && !namedSubscription ? selectVendor(EMPTY_API_KEY_DRAFT, vendor) : EMPTY_API_KEY_DRAFT),
  );
  const [revealed, setRevealed] = React.useState(false);
  const [phase, setPhase] = React.useState<WritePhase>({ kind: 'idle' });
  const [continuation] = React.useState(createContinuationSettlement);
  const clientNonce = React.useRef(sourceClientNonce());
  React.useEffect(() => () => continuation.invalidate(), [continuation]);

  // A method that stopped existing — the last detected candidate was taken over
  // while the dialog was open — falls back rather than rendering nothing.
  const active = methods.includes(method) ? method : methods[0];

  const addedVendors = React.useMemo(
    () => new Set(sources.filter(usableSource).map((source) => providerVendorId(source.vendor))),
    [sources],
  );

  const land = React.useCallback(async (seq: ContinuationTicket, created: SourceCreated | null) => {
    if (continuation.settle(seq, () => setPhase({ kind: 'checking' })) === 'stale') return;
    // The write is committed either way; what the caller reads back is the routing
    // it produced, and a failed read is the screen's to report, not this dialog's.
    await onAdded(created).catch(() => undefined);
    continuation.settle(seq, () => {
      setPhase({ kind: 'idle' });
      onClose();
    });
  }, [continuation, onAdded, onClose]);

  const persist = React.useCallback(async (seq: ContinuationTicket) => {
    if (continuation.settle(seq, () => setPhase({ kind: 'saving' })) === 'stale') return;
    try {
      await land(seq, await modelsApi.createApiKeySource(apiKeySourceCreate(draft, clientNonce.current)));
    } catch (error) {
      continuation.settle(seq, () => setPhase({ kind: 'failed', settled: apiKeyWriteSettled(error) }));
    }
  }, [continuation, draft, land]);

  const submitKey = React.useCallback(async () => {
    const seq = continuation.begin();
    // An unsettled failure is not a verdict. Read the inventory back and adopt the
    // write if it did land, rather than sending a second one that would duplicate it.
    if (phase.kind === 'failed' && !phase.settled) {
      const reconciliation = await reconcileUnknownWrite(
        () => sourceReads.readValue(),
        (rows) => rows.find((row) => row.client_nonce === clientNonce.current),
      );
      if (reconciliation.kind === 'committed') {
        await land(seq, {
          source: reconciliation.value,
          added_to: [],
          adopted_by: reconciliation.value.adopted_by ?? [],
        });
        return;
      }
      // `unread` leaves the outcome exactly as unknown as it already was.
      if (reconciliation.kind === 'absent') await persist(seq);
      return;
    }
    await persist(seq);
  }, [continuation, land, persist, phase, sourceReads]);

  // Editing clears a verdict the server gave about a form that no longer exists.
  // It does NOT clear an unsettled one: that outcome is still unknown, and the
  // nonce it would be reconciled by has to outlive the edit.
  const editDraft = (next: ApiKeySourceDraft) => {
    setDraft(next);
    setPhase((current) => (current.kind === 'failed' && current.settled ? { kind: 'idle' } : current));
  };

  const authorized = (source?: Source, placement?: Adoption) => {
    const seq = continuation.begin();
    void land(seq, source
      ? { source, ...(placement ?? { added_to: [], adopted_by: source.adopted_by ?? [] }) }
      : null);
  };

  const writing = phase.kind === 'saving' || phase.kind === 'checking';
  const busy = writing || phase.kind === 'waitingAuth';
  const keyPreset = apiKeyVendorPreset(draft.vendor);

  const primary = active === 'detected'
    ? {
      label: t('onboarding.providers.addFooterAdd', { count: pendingCount }),
      disabled: pendingCount === 0,
      run: onReviewDetected,
    }
    : active === 'subscription'
      ? {
        label: t('onboarding.providers.addFooterSignInNamed', { name: subscriptionBrand(subscriptionVendor) }),
        disabled: busy,
        run: () => setPhase({ kind: 'waitingAuth' }),
      }
      : {
        label: writing
          ? t('onboarding.providers.addFooterAdding')
          : t(phase.kind === 'failed' ? 'common.retry' : 'onboarding.providers.addFooterAddKey'),
        disabled: busy || !draftComplete(draft),
        run: () => void submitKey(),
      };

  const progressKey: TranslationKey | null = phase.kind === 'saving'
    ? 'onboarding.providers.addProgressSaving'
    : phase.kind === 'checking'
      ? 'onboarding.providers.addProgressChecking'
      : phase.kind === 'waitingAuth'
        ? 'onboarding.providers.addProgressWaitingAuth'
        : null;

  const close = () => {
    if (busy) return;
    continuation.invalidate();
    onClose();
  };

  return (
    <DialogPrimitive.Root open onOpenChange={(next) => !next && close()}>
      <DialogPrimitive.Portal>
        {/* The app's scrim, not one of this screen's own: a dialog over setup should
            dim the way a dialog over anything else does. */}
        <DialogOverlay />
        <DialogPrimitive.Content
          className="setup-add-dialog fixed left-1/2 top-1/2 z-50 flex -translate-x-1/2 -translate-y-1/2 flex-col outline-none"
          onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
        >
          <header className="setup-add-head">
            <div className="flex items-start justify-between gap-3">
              <DialogPrimitive.Title className="setup-add-title">
                {t(more ? 'onboarding.providers.addTitleMore' : 'onboarding.providers.addTitle')}
              </DialogPrimitive.Title>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="setup-add-close size-7 shrink-0"
                // Not 「取消」: the footer already has a button by that name, and two
                // controls announcing the same word is exactly the ambiguity a
                // label exists to remove.
                aria-label={t('common.close')}
                disabled={busy}
                onClick={close}
              >
                <X className="size-[15px]" />
              </Button>
            </div>
            <DialogPrimitive.Description className="setup-add-subtitle">
              {t(more ? 'onboarding.providers.addDescriptionMore' : 'onboarding.providers.addDescription')}
            </DialogPrimitive.Description>
            {/* A segmented group rather than a tablist: these buttons state which
                method is chosen, and what follows is a form rather than a tab
                panel. It is the control `ApiKeySourceForm` already uses for
                protocol, wearing this screen's tokens. */}
            <div role="group" aria-label={t('onboarding.providers.addMethodsLabel')} className="setup-add-methods">
              {methods.map((item) => (
                <button
                  key={item}
                  type="button"
                  className="setup-add-method"
                  aria-pressed={active === item}
                  disabled={busy}
                  onClick={() => setMethod(item)}
                >
                  {t(METHOD_LABEL[item])}
                </button>
              ))}
            </div>
          </header>

          <div className="setup-add-body" data-method={active}>
            {active === 'detected' && (
              <>
                <p className="setup-add-note">{t('onboarding.providers.addDetectedNote')}</p>
                <span className="setup-add-group">{t('onboarding.providers.addDetectedGroup')}</span>
                {detected.map((slot) => (
                  <DetectedRow
                    key={slot.vendor}
                    slot={slot}
                    selected={isSelected(slot)}
                    added={addedVendors.has(slot.vendor)}
                    onToggle={() => onToggleDetected(slot)}
                  />
                ))}
              </>
            )}

            {active === 'subscription' && (
              <>
                <p className="setup-add-note">{t('onboarding.providers.addSubscriptionNote')}</p>
                {SUBSCRIPTION_CHOICES.map((item) => (
                  <button
                    key={item}
                    type="button"
                    className="setup-add-row"
                    data-provider={item}
                    data-state="offer"
                    aria-pressed={subscriptionVendor === item}
                    disabled={busy}
                    onClick={() => setSubscriptionVendor(item)}
                  >
                    <span className="setup-add-row-logo"><VendorGlyph vendor={item} /></span>
                    <span className="setup-add-row-copy">
                      <span className="setup-add-row-name">
                        {t('onboarding.providers.addSignInNamed', { name: subscriptionBrand(item) })}
                      </span>
                    </span>
                    {subscriptionVendor === item && (
                      <Check className="setup-add-row-check" strokeWidth={2.2} aria-hidden="true" />
                    )}
                  </button>
                ))}
              </>
            )}

            {active === 'apiKey' && (
              <ApiKeySourceForm
                draft={draft}
                disabled={writing}
                revealed={revealed}
                keyLabel={keyPreset
                  ? t('onboarding.providers.addKeyLabelNamed', { name: keyPreset.label })
                  : undefined}
                onChange={editDraft}
                onToggleReveal={() => setRevealed((value) => !value)}
                onSubmit={() => void submitKey()}
              />
            )}
          </div>

          <footer className="setup-add-foot">
            <span className="setup-add-status" role="status" aria-live="polite">
              {progressKey && (
                <>
                  <LoaderCircle className="setup-add-status-icon motion-safe:animate-spin" aria-hidden="true" />
                  {t(progressKey)}
                </>
              )}
              {phase.kind === 'failed' && (
                <>
                  <CircleX className="setup-add-status-icon setup-add-status-icon--error" aria-hidden="true" />
                  <span className="setup-add-status-error">{t('onboarding.providers.addErrorPreserved')}</span>
                </>
              )}
            </span>
            <Button type="button" variant="outline" size="sm" disabled={busy} onClick={close}>
              {t('common.cancel')}
            </Button>
            <Button type="button" variant="brand" size="sm" disabled={primary.disabled} onClick={primary.run}>
              {primary.label}
            </Button>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>

      {/* Launched, not switched to: the authorization flow owns its own frame, its
          channel choice, its poll, its deadline and its cancellation. This dialog
          only waits under it, which is why C5's still-frame rule is untouched. */}
      {phase.kind === 'waitingAuth' && (
        <OAuthConnectDialog
          open
          vendor={subscriptionVendor}
          sources={sources}
          onClose={() => setPhase({ kind: 'idle' })}
          onConnected={authorized}
        />
      )}
    </DialogPrimitive.Root>
  );
};
