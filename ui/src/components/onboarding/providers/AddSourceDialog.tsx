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
import { Check, CircleX, KeyRound, LoaderCircle, X } from 'lucide-react';
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
import { BLOCKED_REASON_FALLBACK_KEY } from '@/components/settings/models/migrationGrouping';
import {
  createContinuationSettlement,
  type ContinuationTicket,
} from '@/components/settings/models/mutationSettlement';
import { modelsApi, type Adoption, type SourceCreated } from '@/components/settings/models/modelsApi';
import { OAuthConnectDialog } from '@/components/settings/models/OAuthConnectDialog';
import { reconcileUnknownWrite } from '@/components/settings/models/reconcileUnknownWrite';
import { serverText } from '@/components/settings/models/serverCopy';
import { subscriptionChooser } from '@/components/settings/models/subscriptionOptions';
import type { Source } from '@/components/settings/models/types';
import { VendorGlyph } from '@/components/settings/models/vendorGlyph';
import { providerBrandLabel } from '@/components/settings/providers/providerIdentity';
import type { TranslationKey } from '@/i18n/types';

import { type ProviderSlot } from './providerStage';

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
  onToggle: () => void;
}> = ({ slot, selected, onToggle }) => {
  const { t } = useTranslation();
  // Detected, and not takeable from here. The row stays — this list is what the
  // screen found, and a credential that vanished because nobody may act on it is a
  // credential the person will add a second copy of.
  const blocked = slot.reasons.length > 0;
  const detail = [
    slot.mask ? t('onboarding.providers.cardKeyDetected', { mask: slot.mask }) : '',
    ...slot.reasons.map((key) => serverText(t, key, BLOCKED_REASON_FALLBACK_KEY) ?? ''),
  ].filter(Boolean).join(' · ');
  return (
    <button
      type="button"
      className="setup-add-row"
      data-provider={slot.vendor}
      data-state="detected"
      {...(blocked ? { 'data-blocked': 'true' } : {})}
      disabled={blocked}
      {...(blocked ? {} : { 'aria-pressed': selected })}
      aria-label={blocked
        ? [slot.label, detail].filter(Boolean).join(' · ')
        : t('onboarding.providers.addDetectedSelectNamed', { name: slot.label })}
      onClick={onToggle}
    >
      <span className="setup-add-row-logo">
        {slot.brand ? <VendorGlyph vendor={slot.brand} /> : <KeyRound size={18} aria-hidden="true" />}
      </span>
      <span className="setup-add-row-copy">
        <span className="setup-add-row-name">{slot.label}</span>
        {detail && <span className="setup-add-row-detail">{detail}</span>}
      </span>
      {selected && <Check className="setup-add-row-check" strokeWidth={2.2} aria-hidden="true" />}
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
  /** Whether the screen still admits a write. It can turn false while this is open —
   *  the engine stopped, or the read that said it was serving failed — and then the
   *  submit is what has to refuse. An authorization already in flight is left alone:
   *  it owns its own outcome, and this frame stays up to report it. */
  writable: boolean;
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
  writable,
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

  // The detected list exists as discovery whatever it holds, but it only OPENS the
  // dialog when something in it can be taken over. A person who pressed 「添加」 and
  // landed on rows that all explain why they cannot be imported has been handed an
  // answer to a question they did not ask, with nothing to press; the tab is still
  // right there, saying what is on this machine.
  const [method, setMethod] = React.useState<AddSourceMethod>(
    namedSubscription
      ? 'subscription'
      : vendor
        ? 'apiKey'
        : detected.some((slot) => slot.backends.length > 0)
          ? 'detected'
          : 'subscription',
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
  // Set when an authorization really produced a source. It is what tells that
  // flow's own close apart from a cancellation: both arrive the same way.
  const authorizedLanded = React.useRef(false);
  // Permission as of now, not as of the press. A submit that first reads the inventory
  // back can be inside that await when the engine stops, and the value the press saw is
  // by then a memory of a permission rather than one. Everything that SENDS reads this.
  const writableRef = React.useRef(writable);
  writableRef.current = writable;
  React.useEffect(() => () => continuation.invalidate(), [continuation]);

  // A method that stopped existing — the last detected candidate was taken over
  // while the dialog was open — falls back rather than rendering nothing.
  const active = methods.includes(method) ? method : methods[0];


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
    // The actual boundary of the POST, and the last place the answer is still current.
    // Refusing here changes nothing else on purpose: the draft, the nonce and an
    // unsettled failure all stay exactly as they were, so the outcome nobody knows yet
    // is still reconcilable from the same identity once the engine is back. What it will
    // not do is send. There is no queued attempt behind this — recovery is another press.
    if (!writableRef.current) return;
    if (continuation.settle(seq, () => setPhase({ kind: 'saving' })) === 'stale') return;
    try {
      await land(seq, await modelsApi.createApiKeySource(apiKeySourceCreate(draft, clientNonce.current)));
    } catch (error) {
      continuation.settle(seq, () => setPhase({ kind: 'failed', settled: apiKeyWriteSettled(error) }));
    }
  }, [continuation, draft, land]);

  const submitKey = React.useCallback(async () => {
    // The footer below already refuses this; refusing it here too is what makes a
    // write against an engine the screen no longer vouches for unrepresentable
    // rather than merely hard to reach.
    if (!writable) return;
    const seq = continuation.begin();
    // An unsettled failure is not a verdict. Read the inventory back and adopt the
    // write if it did land, rather than sending a second one that would duplicate it.
    if (phase.kind === 'failed' && !phase.settled) {
      const reconciliation = await reconcileUnknownWrite(
        () => sourceReads.readValue(),
        (rows) => rows.find((row) => row.client_nonce === clientNonce.current),
      );
      // A source that is there is a receipt, and a receipt is reported whatever the
      // screen now admits: permission governs the NEXT write, never the delivery of one
      // that already happened. Withdrawing it here would hide a credential that exists.
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
  }, [continuation, land, persist, phase, sourceReads, writable]);

  // Editing clears a verdict the server gave about a form that no longer exists.
  // It does NOT clear an unsettled one: that outcome is still unknown, and the
  // nonce it would be reconciled by has to outlive the edit.
  const editDraft = (next: ApiKeySourceDraft) => {
    setDraft(next);
    setPhase((current) => (current.kind === 'failed' && current.settled ? { kind: 'idle' } : current));
  };

  const authorized = (source?: Source, placement?: Adoption) => {
    // No source is 「你手上的行已经过期」, not 「新增了一个来源」. The shipped dialog fires
    // that argument-less call on every terminal arrival — failures, cancellations,
    // cleanup, a flow resolved after its frame closed — so refresh behind it and stay
    // where we are. Landing on it would close this dialog on someone's cancelled
    // sign-in and report it as a provider they added.
    if (!source) {
      // Except immediately behind this flow's own landing. A success is reported by
      // two calls in one breath — the source, then the argument-less one — and the
      // second is not news about anything: the row it would say had gone stale is
      // the one just created, and the read that describes it is already in flight.
      // Asking again sends a second inventory read racing the first, so which of
      // the two the stage ends up drawing is decided by whichever answers last.
      if (authorizedLanded.current) return;
      void onAdded(null).catch(() => undefined);
      return;
    }
    // A source did arrive — and the flow's own success panel is still on screen,
    // holding the report of where it landed for the 1400ms it owns. Tearing that
    // down here to show this dialog's spinner would take the one thing the person
    // was waiting to read. So read back behind it, and let its close be what
    // closes this frame. The shipped host does exactly this.
    authorizedLanded.current = true;
    void onAdded({ source, ...(placement ?? { added_to: [], adopted_by: source.adopted_by ?? [] }) })
      .catch(() => undefined);
  };

  const writing = phase.kind === 'saving' || phase.kind === 'checking';
  const busy = writing || phase.kind === 'waitingAuth';
  // An unsettled failure has an unknown outcome and a nonce that must outlive the
  // retry. Editing under it would produce a form whose edits the retry can discard
  // without saying so — reconciliation adopts the row the ORIGINAL draft wrote — so
  // the fields stay locked until that retry has an answer.
  const unsettled = phase.kind === 'failed' && !phase.settled;
  const keyBlocked = busy || !writable || !draftComplete(draft);
  const keyPreset = apiKeyVendorPreset(draft.vendor);

  // Every method's primary is a write — a key saved, a sign-in started, a batch handed
  // to the takeover — so every one of them needs the screen to still admit one. The
  // frame stays exactly as it is: what is on it was worth opening and is still worth
  // reading, and the reason it cannot be submitted is on the gateway card behind it,
  // next to the only control that can do anything about it.
  const primary = active === 'detected'
    ? {
      label: t('onboarding.providers.addFooterAdd', { count: pendingCount }),
      disabled: pendingCount === 0 || !writable,
      run: onReviewDetected,
    }
    : active === 'subscription'
      ? {
        label: t('onboarding.providers.addFooterSignInNamed', { name: subscriptionBrand(subscriptionVendor) }),
        disabled: busy || !writable,
        run: () => setPhase({ kind: 'waitingAuth' }),
      }
      : {
        label: writing
          ? t('onboarding.providers.addFooterAdding')
          : t(phase.kind === 'failed' ? 'common.retry' : 'onboarding.providers.addFooterAddKey'),
        disabled: keyBlocked,
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
    // An unknown outcome is closable — holding someone in a dialog because a request
    // timed out is its own failure — but it may not be closed by forgetting it. The
    // nonce that identifies this write lives in this frame, so a source it did create
    // would become unattributable the moment the frame went away: the parent would
    // still show nothing, the next attempt would carry a new nonce, and that attempt
    // is the duplicate reconciliation exists to prevent.
    //
    // So the read is started here and not awaited: it runs outside the continuation,
    // which this very close invalidates, and reports through the same `onAdded` a
    // settled landing uses. A read that fails leaves the outcome exactly as unknown
    // as it already was, which the parent's own refresh is what reports.
    if (unsettled) {
      const nonce = clientNonce.current;
      void reconcileUnknownWrite(
        () => sourceReads.readValue(),
        (rows) => rows.find((row) => row.client_nonce === nonce),
      ).then((reconciliation) => onAdded(
        reconciliation.kind === 'committed'
          ? {
            source: reconciliation.value,
            added_to: [],
            adopted_by: reconciliation.value.adopted_by ?? [],
          }
          : null,
      )).catch(() => undefined);
    }
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
                disabled={writing || unsettled}
                revealed={revealed}
                keyLabel={keyPreset
                  ? t('onboarding.providers.addKeyLabelNamed', { name: keyPreset.label })
                  : undefined}
                onChange={editDraft}
                onToggleReveal={() => setRevealed((value) => !value)}
                // Enter is the footer button, so it obeys the footer button's rule:
                // an incomplete draft submitted from the keyboard is the same write
                // the disabled control exists to refuse.
                onSubmit={() => { if (!keyBlocked) void submitKey(); }}
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
          onClose={() => {
            if (!authorizedLanded.current) {
              // Cancelled, failed, or abandoned: back to the frame it was launched
              // from, with the method and the draft it was launched with.
              setPhase({ kind: 'idle' });
              return;
            }
            authorizedLanded.current = false;
            continuation.invalidate();
            onClose();
          }}
          onConnected={authorized}
        />
      )}
    </DialogPrimitive.Root>
  );
};
