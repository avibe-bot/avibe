// One claim from this tab, from the press to its end: start it, leave for avibe.bot, and, back on
// the page (or on reopening it), follow it by reading its state. It never starts a second claim
// beside an open one, and a read that fails because this browser is no longer admitted is not a
// failure of the claim: binding succeeded and the page must be opened at the new address.
import { useCallback, useEffect, useRef, useState } from 'react';

import { DevicesApiError, LostAnswer, devicesApi, type ClaimForm, type ClaimState } from './api';
import {
  CLAIM_BINDING, CLAIM_OVER, CLAIM_RETRYING, claimLocale, clearClaimMarker, readClaimMarker, submitClaimForm, writeClaimMarker,
  type ClaimKind, type ReturnRefusal,
} from './claimFlow';

const POLL_MS = 1500;
const HOST_MISMATCH = 'remote_access_host_mismatch';

/** How a claim ended, as the local API said it. */
export type ClaimEnd =
  | { state: 'bound'; claim: ClaimState }
  /** Refused, expired, cancelled: the code, and what the read carried. */
  | { state: 'refused'; code: string; claim?: ClaimState; claimId?: string; seconds?: number };

export type ClaimFlow = {
  /** `sending`: the start is on its way. `waiting`: open, not approved at avibe.bot yet (a state
   *  this page does not know is read as still open, never as over). `retrying`: open, and the
   *  instance is trying avibe.bot again. `binding`: approved, this computer is binding. */
  phase: 'idle' | 'sending' | 'waiting' | 'retrying' | 'binding';
  /** The claim this tab follows, kept for what only its starter may do. */
  claimId: string | null;
  /** Binding succeeded and this browser lost its admission with it. */
  lost: { host?: string } | null;
  start: () => Promise<void>;
  cancel: (claimId?: string) => Promise<void>;
};

const routes = {
  claim: {
    start: (locale: string) => devicesApi.startClaim(locale),
    read: (id: string) => devicesApi.claim(id),
    cancel: (id: string) => devicesApi.cancelClaim(id),
  },
  rebind: {
    start: (locale: string) => devicesApi.startRebind(locale),
    read: (id: string) => devicesApi.rebindClaim(id),
    cancel: (id: string) => devicesApi.cancelRebind(id),
  },
} as const satisfies Record<ClaimKind, { start: (locale: string) => Promise<ClaimForm>; read: (id: string) => Promise<ClaimState>; cancel: (id: string) => Promise<void> }>;

export function useClaimFlow({ kind, locale, onEnd, returned = null, submit = submitClaimForm }: {
  kind: ClaimKind;
  locale: string;
  onEnd: (end: ClaimEnd) => void;
  /** The page was opened by a return from avibe.bot that was refused: the claim this tab kept
   *  cannot finish any more. It is withdrawn and not followed; the caller says why. */
  returned?: ReturnRefusal | null;
  /** Leaves the page for avibe.bot; replaced in tests. */
  submit?: (form: ClaimForm) => void;
}): ClaimFlow {
  const [phase, setPhase] = useState<ClaimFlow['phase']>('idle');
  const [refusedReturn] = useState(() => {
    const marker = returned ? readClaimMarker() : null;
    return marker?.kind === kind ? marker.id : null;
  });
  const [claimId, setClaimId] = useState<string | null>(() => {
    const marker = returned ? null : readClaimMarker();
    return marker?.kind === kind ? marker.id : null;
  });
  const [following, setFollowing] = useState<boolean>(() => !returned && readClaimMarker()?.kind === kind);
  const [lost, setLost] = useState<ClaimFlow['lost']>(null);
  const ended = useRef(onEnd);
  useEffect(() => { ended.current = onEnd; }, [onEnd]);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const { start: startRoute, read, cancel: cancelRoute } = routes[kind];

  useEffect(() => {
    if (!refusedReturn) return;
    clearClaimMarker();
    void cancelRoute(refusedReturn).catch(() => undefined);
  }, [cancelRoute, refusedReturn]);

  useEffect(() => {
    if (!following || !claimId) return undefined;
    let alive = true;
    let timer: number | undefined;
    const finish = (end: ClaimEnd) => {
      setFollowing(false);
      setPhase('idle');
      ended.current(end);
    };
    const tick = async () => {
      try {
        const claim = await read(claimId);
        if (!alive) return;
        if (claim.hostname) {
          const marker = readClaimMarker();
          if (marker?.id === claimId && marker.host !== claim.hostname) writeClaimMarker({ ...marker, host: claim.hostname });
        }
        if (claim.state === 'bound') {
          // The marker stays with a rebind's starter until the page has shown the result.
          if (kind === 'claim') clearClaimMarker();
          finish({ state: 'bound', claim });
          return;
        }
        if (CLAIM_BINDING.has(claim.state)) setPhase('binding');
        else if (!CLAIM_OVER.has(claim.state)) setPhase(claim.state === CLAIM_RETRYING ? 'retrying' : 'waiting');
        else {
          if (kind === 'claim') clearClaimMarker();
          finish({
            state: 'refused',
            code: claim.code ?? (claim.state === 'cancelling' || claim.state === 'declined' ? 'declined' : claim.state),
            claim,
            claimId,
            seconds: claim.retryAfterSeconds,
          });
          return;
        }
      } catch (error) {
        if (!alive) return;
        if (error instanceof DevicesApiError) {
          if (error.code === HOST_MISMATCH) {
            // Not a failure: this address is no longer this computer's own. Stop reading.
            setLost({ host: readClaimMarker()?.host });
            setFollowing(false);
            setPhase('idle');
            return;
          }
          if (kind === 'claim') clearClaimMarker();
          finish({ state: 'refused', code: error.code, claimId });
          return;
        }
        // A lost answer says nothing about the claim: read again.
      }
      timer = window.setTimeout(() => { void tick(); }, POLL_MS);
    };
    void tick();
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [claimId, following, kind, read]);

  const start = useCallback(async () => {
    if (phase !== 'idle') return;
    setPhase('sending');
    try {
      const form = await startRoute(claimLocale(locale));
      if (!mounted.current) {
        // The person left this page while the request was on its way: nothing takes them to
        // avibe.bot now, and the claim they did not wait for is withdrawn.
        void cancelRoute(form.claimId).catch(() => undefined);
        return;
      }
      writeClaimMarker({ kind, id: form.claimId, at: Date.now(), away: true });
      setClaimId(form.claimId);
      submit(form);
      // The page is leaving; if it does not (a test, a blocked navigation), follow the claim here.
      setFollowing(true);
      setPhase('waiting');
    } catch (error) {
      if (!mounted.current) return;
      setPhase('idle');
      if (error instanceof DevicesApiError) {
        const other = typeof error.body.claim_id === 'string' ? error.body.claim_id : undefined;
        const seconds = typeof error.body.retry_after_seconds === 'number' ? error.body.retry_after_seconds : undefined;
        ended.current({ state: 'refused', code: error.code, claimId: other, seconds });
      } else {
        ended.current({ state: 'refused', code: error instanceof LostAnswer ? 'no_answer' : 'unknown' });
      }
    }
  }, [cancelRoute, kind, locale, phase, startRoute, submit]);

  const cancel = useCallback(async (id?: string) => {
    const target = id ?? claimId;
    if (!target) return;
    try {
      await cancelRoute(target);
    } catch {
      // The read that follows says where the claim stands.
    }
    if (target === claimId) {
      // Its own read ends the flow as declined.
      setFollowing(true);
    } else {
      ended.current({ state: 'refused', code: 'declined' });
    }
  }, [cancelRoute, claimId]);

  return { phase, claimId, lost, start, cancel };
}
