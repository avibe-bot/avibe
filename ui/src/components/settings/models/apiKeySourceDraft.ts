// What an API-key source is, before it is a request.
//
// Split out of `AddApiKeyDialog` when setup needed the same fields inside a
// different frame. The dialog kept five `useState` calls and read them back in
// `draft()`; a second host doing the same would be a second place the vendor
// rule, the endpoint default and the submit condition could drift. So the shape
// and its rules live here, the fields that render it live in
// `ApiKeySourceForm.tsx`, and a host owns only the state and what it does with
// it — which is what lets setup preserve a half-typed key across a tab switch
// without knowing anything about what a key is.
//
// Values, not a component, because a module exporting both opts out of fast
// refresh — the same split `vendorMarks.ts` and `vendorGlyph.tsx` already make.
import { apiKeyVendorPreset, CUSTOM_VENDOR } from './apiKeyVendors';
import { apiFailure } from './modelsApi';
import {
  SOURCE_DISPLAY_NAME_MAX_LENGTH,
  type ApiKeySourceCreate,
  type SourceProtocol,
} from './types';
import { optionalTrimmedTextWithin } from './validation';

/** The fields, exactly as typed. Trimming happens once, on the way out. */
export type ApiKeySourceDraft = {
  vendor: string;
  displayName: string;
  baseUrl: string;
  apiKey: string;
  /** What the person chose; a catalog vendor overrides it (see `draftProtocol`). */
  protocol: SourceProtocol;
};

/** 自定义 is where the field opens: it is the absence of a vendor, so nothing is
 *  assumed about the endpoint or the protocol until one is chosen. */
export const EMPTY_API_KEY_DRAFT: ApiKeySourceDraft = {
  vendor: CUSTOM_VENDOR,
  displayName: '',
  baseUrl: '',
  apiKey: '',
  protocol: 'openai_chat',
};

/**
 * Choose a vendor.
 *
 * Re-selecting the current one changes nothing, which is what preserves a
 * hand-edited endpoint and protocol; a different vendor starts from that
 * vendor's own defaults rather than from the last one's leftovers. The key is
 * never touched either way — it belongs to the person, not to the catalog.
 */
export function selectVendor(draft: ApiKeySourceDraft, vendor: string): ApiKeySourceDraft {
  if (vendor === draft.vendor) return draft;
  return {
    ...draft,
    vendor,
    baseUrl: apiKeyVendorPreset(vendor)?.official_base_url ?? '',
    protocol: 'openai_chat',
  };
}

/** The protocol that will actually be sent: a catalog vendor publishes one, so
 *  for those the segmented control is a statement rather than a question. */
export const draftProtocol = (draft: ApiKeySourceDraft): SourceProtocol =>
  apiKeyVendorPreset(draft.vendor)?.protocol ?? draft.protocol;

/** A name is optional; one that is only too long is not. */
export const draftNameValid = (draft: ApiKeySourceDraft): boolean =>
  optionalTrimmedTextWithin(draft.displayName, SOURCE_DISPLAY_NAME_MAX_LENGTH);

/** Whether there is enough here to save. */
export const draftComplete = (draft: ApiKeySourceDraft): boolean =>
  Boolean(draft.baseUrl.trim() && draft.apiKey.trim()) && draftNameValid(draft);

/**
 * A nonce the client can recognise its own write by.
 *
 * It is what makes an unknown outcome answerable: after a timeout the source
 * list is read back and matched on this, so a write that landed is adopted
 * rather than repeated.
 */
export const sourceClientNonce = (): string => {
  const uuid = globalThis.crypto.randomUUID?.();
  if (uuid) return `scn_${uuid.replaceAll('-', '').toLowerCase()}`;
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return `scn_${Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')}`;
};

/**
 * Whether a failed create really decided the write's outcome.
 *
 * Only a server-named 4xx does, and 409 not even then: everything else — a
 * timeout, a 5xx, a dropped connection — leaves a request that may have been
 * committed. Repeating one of those is how a second identical source appears, so
 * an unsettled write is reconciled against `client_nonce` instead
 * (`reconcileUnknownWrite`). Shared because both hosts of the same form have to
 * draw the same line; a second copy is how one of them starts retrying blind.
 */
export const apiKeyWriteSettled = (error: unknown): boolean => {
  const failure = apiFailure(error);
  const status = failure?.responseStatus;
  return Boolean(failure?.serverNamed) && status !== undefined
    && status >= 400 && status < 500 && status !== 409;
};

/**
 * The request.
 *
 * `save_unverified` is deliberate and shared by both hosts: a credential is saved
 * on the person's word and verified afterwards, so a provider that is slow or
 * briefly down does not cost them the key they just pasted.
 */
export const apiKeySourceCreate = (
  draft: ApiKeySourceDraft,
  clientNonce: string,
): ApiKeySourceCreate => ({
  kind: 'api_key',
  vendor: draft.vendor,
  ...(draft.displayName.trim() ? { display_name: draft.displayName.trim() } : {}),
  base_url: draft.baseUrl.trim(),
  key: draft.apiKey.trim(),
  protocol: draftProtocol(draft),
  client_nonce: clientNonce,
  save_unverified: true,
});
