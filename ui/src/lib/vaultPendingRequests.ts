import type { VaultRequest } from '@/context/ApiContext';

/** What a pending request asks the user to do: its card's type, else the row's. */
export const vaultRequestReviewType = (request: VaultRequest) =>
  (request.card as { request_type?: string } | null)?.request_type ?? request.request_type;

const REVIEWABLE = new Set(['access', 'sign', 'provision', 'delete']);

/** A request the user answers from a pending list: use, sign, provide or delete a secret. */
export const isReviewableVaultRequest = (request: VaultRequest) => REVIEWABLE.has(vaultRequestReviewType(request) ?? '');

/** The requests still waiting at `now` (ms): one past its `expires_at` can no longer be answered. */
export function liveVaultRequests(requests: VaultRequest[], now: number): VaultRequest[] {
  return requests.filter((request) => {
    const end = request.expires_at ? Date.parse(request.expires_at) : NaN;
    return Number.isNaN(end) || end > now;
  });
}
