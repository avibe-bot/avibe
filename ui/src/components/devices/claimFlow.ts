// The browser side of a claim (docs/plans/personal-installation-claims-runtime.md): the top-level
// form that takes the person to avibe.bot once, and what this tab remembers across that trip.
// The claim's id, its form and, later, the new address come only from the local API's answers;
// nothing is ever read from the address bar.
import type { ClaimForm } from './api';

const KEY = 'avibe.devices.claim';
/** A claim this tab started is remembered this long: longer than any claim lives. */
const MARKER_TTL_MS = 30 * 60_000;

/** `claim`: turning remote access on. `rebind`: binding again, from the connection page. */
export type ClaimKind = 'claim' | 'rebind';
export type ClaimMarker = {
  kind: ClaimKind;
  id: string;
  at: number;
  /** The address the local API reported while the claim was being accepted. */
  host?: string;
};

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function readClaimMarker(now: number = Date.now()): ClaimMarker | null {
  try {
    const raw = storage()?.getItem(KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<ClaimMarker> | null;
    if (!value || (value.kind !== 'claim' && value.kind !== 'rebind') || typeof value.id !== 'string' || typeof value.at !== 'number') return null;
    if (now - value.at > MARKER_TTL_MS || value.at > now + 60_000) return null;
    return { kind: value.kind, id: value.id, at: value.at, host: typeof value.host === 'string' ? value.host : undefined };
  } catch {
    return null;
  }
}

export function writeClaimMarker(marker: ClaimMarker): void {
  try {
    storage()?.setItem(KEY, JSON.stringify(marker));
  } catch {
    // Without session storage the claim still runs; only reopening the page forgets it.
  }
}

export function clearClaimMarker(): void {
  try {
    storage()?.removeItem(KEY);
  } catch {
    // Nothing to clear.
  }
}

/**
 * Leaves for avibe.bot: sets the document's referrer policy as the instance asks and submits a
 * top-level form with exactly the fields it gave. No fetch, no frame, no destination of the page's
 * own choosing.
 */
export function submitClaimForm(form: ClaimForm): void {
  let policy = document.querySelector<HTMLMetaElement>('meta[name="referrer"]');
  if (!policy) {
    policy = document.createElement('meta');
    policy.name = 'referrer';
    document.head.append(policy);
  }
  policy.content = form.referrerPolicy;
  const element = document.createElement('form');
  element.method = form.method;
  element.action = form.action;
  element.hidden = true;
  for (const [name, value] of Object.entries(form.fields)) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    element.append(input);
  }
  document.body.append(element);
  element.submit();
}

/** The states of a claim read in which it is still on its way. */
export const CLAIM_OPEN = new Set(['starting', 'pending', 'waiting']);
export const CLAIM_BINDING = new Set(['accepting', 'pairing']);
