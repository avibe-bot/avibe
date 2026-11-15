// One full request before any approval decision (protected Bitcoin contract rev0.4 §4.3). A pending
// list may carry a proof-free summary of a new signing request: no name, card, delivery or session.
// `card: null` there does not mean standard, unprotected or browser-free, so nothing is decided from
// it. The selected request is read in full, uncached, by its id; protection, the passkey and the
// browser hand-off are decided from that read, and the shared approval dialog is mounted on it.
import { useEffect, useState } from 'react';

import { useApi, type VaultRequest } from '@/context/ApiContext';

/** A list row whose card is absent: a new-kind summary (or an old request whose card failed to load).
 *  Either way it must be read in full before anything is decided or shown as protected. */
export const vaultRequestNeedsFullRead = (request: VaultRequest): boolean => request.card == null;

export type FullVaultRequest =
  | { phase: 'loading' }
  | { phase: 'ready'; request: VaultRequest }
  | { phase: 'failed'; code: string | null };

/** Why a read cannot be used: anything but this exact request, still pending. */
type FullRead = Exclude<FullVaultRequest, { phase: 'loading' }>;

function checkFullRead(id: string, reply: { ok?: boolean; request?: VaultRequest; code?: string } | null): FullRead {
  const request = reply?.request;
  if (!reply || reply.ok === false || !request) return { phase: 'failed', code: reply?.code ?? null };
  if (request.id !== id) return { phase: 'failed', code: 'request_mismatch' };
  if (request.status !== 'pending') return { phase: 'failed', code: 'request_not_pending' };
  return { phase: 'ready', request };
}

/**
 * The full request for the one selected, or that request itself when it already is full. The read
 * belongs to this id and this mount: a new selection, a close or an unmount drops a late answer, and
 * `retry` reads again. Nothing is prefetched for rows that are not selected.
 */
export function useFullVaultRequest(selected: VaultRequest | null): { state: FullVaultRequest | null; retry: () => void } {
  const api = useApi();
  const id = selected?.id ?? null;
  const needsRead = selected ? vaultRequestNeedsFullRead(selected) : false;
  const [attempt, setAttempt] = useState(0);
  const [read, setRead] = useState<{ id: string; attempt: number; state: FullVaultRequest } | null>(null);
  useEffect(() => {
    if (!id || !needsRead) return undefined;
    const controller = new AbortController();
    api.getVaultRequest(id, { signal: controller.signal })
      .then((reply) => checkFullRead(id, reply), () => ({ phase: 'failed', code: null }) as FullRead)
      .then((state) => { if (!controller.signal.aborted) setRead({ id, attempt, state }); });
    return () => controller.abort();
  }, [api, id, needsRead, attempt]);
  if (!selected) return { state: null, retry: () => undefined };
  if (!needsRead) return { state: { phase: 'ready', request: selected }, retry: () => undefined };
  const state = read && read.id === id && read.attempt === attempt ? read.state : { phase: 'loading' as const };
  return { state, retry: () => setAttempt((n) => n + 1) };
}

/** Read one request by id (a focused or handed-off request, which may not be in any list). */
export async function readFullVaultRequest(
  api: { getVaultRequest: (id: string, opts?: { signal?: AbortSignal }) => Promise<{ ok?: boolean; request?: VaultRequest; code?: string }> },
  id: string,
  signal?: AbortSignal,
): Promise<FullRead> {
  try {
    return checkFullRead(id, await api.getVaultRequest(id, { signal }));
  } catch {
    return { phase: 'failed', code: null };
  }
}
