import { useCallback, useEffect, useRef, type FC } from 'react';

import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { apiFetch, withApiDeadline } from '@/lib/apiFetch';
import {
  hasNativeCapability,
  nativeAppInfo,
  nativeSystemScheduleCancel,
  nativeSystemScheduleRequest,
  type NativeSystemScheduleCancel,
  type NativeSystemScheduleReceipt,
  type NativeSystemScheduleRequest,
} from '@/lib/native';
import { onPageReactivated } from '@/lib/pageActivity';

const MAX_CLAIMS_PER_RECONCILE = 16;
const CLAIM_TIMEOUT_MS = 15_000;
const RECEIPT_TIMEOUT_MS = 15_000;
const PERMANENT_RECEIPT_ERRORS = new Set([
  'invalid_receipt',
  'receipt_conflict',
  'receipt_mismatch',
  'stale_receipt',
]);
const RETRY_NATIVE_RECEIPT_ERRORS = new Set([
  'local_state_write_failed',
  'local_state_unavailable',
]);

type NativeIdentity = {
  instanceId: string;
  deviceId: string;
};

type ScheduleClaim = {
  action: 'create' | 'cancel';
  generation: number;
  purpose: 'user';
  leaseToken: string;
  leaseExpiresAt: string;
  request: NativeSystemScheduleRequest | NativeSystemScheduleCancel;
};

type PendingReceipt = NativeIdentity & {
  generation: number;
  leaseToken: string;
  receipt: NativeSystemScheduleReceipt;
};

async function readIdentity(): Promise<NativeIdentity | null> {
  const info = await nativeAppInfo();
  const instanceId = info?.instance?.id?.trim() ?? '';
  const deviceId = info?.device?.id?.trim() ?? '';
  return instanceId && deviceId ? { instanceId, deviceId } : null;
}

async function claimNext(
  identity: NativeIdentity,
  excludeLeaseTokens: string[],
  allowCreate: boolean,
): Promise<ScheduleClaim | null> {
  return withApiDeadline(CLAIM_TIMEOUT_MS, undefined, async (signal) => {
    const response = await apiFetch('/api/system-schedule/claim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instance_id: identity.instanceId,
        device_id: identity.deviceId,
        exclude_lease_tokens: excludeLeaseTokens,
        allow_create: allowCreate,
      }),
      signal,
    });
    if (!response.ok) throw new Error(`system schedule claim failed (${response.status})`);
    const payload = await response.json();
    return payload?.claim ?? null;
  });
}

async function deliverReceipt(pending: PendingReceipt): Promise<'accepted' | 'discard' | 'retry'> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), RECEIPT_TIMEOUT_MS);
  try {
    const response = await apiFetch('/api/system-schedule/receipt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instance_id: pending.instanceId,
        device_id: pending.deviceId,
        lease_token: pending.leaseToken,
        receipt: pending.receipt,
      }),
      signal: controller.signal,
    });
    if (response.ok) return 'accepted';
    if (response.status === 400 || response.status === 409) {
      const payload = await response.clone().json().catch(() => null);
      if (PERMANENT_RECEIPT_ERRORS.has(payload?.error)) return 'discard';
    }
  } catch {
    // The lease and receipt remain in memory for the next claim (page open,
    // reactivation, an Agent turn end or a stream reconnect).
  } finally {
    window.clearTimeout(timeout);
  }
  return 'retry';
}

/** Forwards durable Runtime requests through independent trusted iOS bridge leases. */
export const SystemScheduleForwarder: FC = () => {
  const { connectWorkbenchEvents } = useApi();
  const { capabilities } = useInstanceAuthorization();
  const active = capabilities.can_chat && hasNativeCapability('systemSchedule');
  const enabledRef = useRef(false);
  const claimMutexRef = useRef(false);
  // A reconcile asked for while another runs (for example when a Native call
  // finishes): run once more after it, instead of dropping the request.
  const rerunRequestedRef = useRef(false);
  const inFlightLeasesRef = useRef(new Map<
    string,
    { leaseToken: string; action: ScheduleClaim['action'] }
  >());
  const pendingReceiptsRef = useRef(new Map<string, PendingReceipt>());
  const receiptDeliveryByLeaseRef = useRef(new Map<string, Promise<void>>());
  const reconcileRef = useRef<() => Promise<void>>(async () => undefined);

  const leaseKey = useCallback((
    identity: NativeIdentity,
    generation: number,
    leaseToken: string,
  ) => (
    `${identity.instanceId}\u0000${identity.deviceId}\u0000${generation}\u0000${leaseToken}`
  ), []);

  const startClaim = useCallback((identity: NativeIdentity, claim: ScheduleClaim) => {
    const key = leaseKey(identity, claim.generation, claim.leaseToken);
    if (
      inFlightLeasesRef.current.has(key)
      || pendingReceiptsRef.current.has(key)
    ) {
      return;
    }
    inFlightLeasesRef.current.set(key, {
      leaseToken: claim.leaseToken,
      action: claim.action,
    });
    void (async () => {
      let reconcileImmediately = true;
      try {
        const receipt = claim.action === 'create'
          ? await nativeSystemScheduleRequest(claim.request as NativeSystemScheduleRequest)
          : await nativeSystemScheduleCancel(claim.request as NativeSystemScheduleCancel);
        if (receipt?.error && RETRY_NATIVE_RECEIPT_ERRORS.has(receipt.error.code)) {
          // Native cannot yet read or commit the item's management record.
          // Keep the Runtime lease sticky so the next claim re-enters Native
          // recovery instead of treating an uncertain effect as terminal.
          reconcileImmediately = false;
          return;
        }
        if (receipt) {
          pendingReceiptsRef.current.set(key, {
            ...identity,
            generation: claim.generation,
            leaseToken: claim.leaseToken,
            receipt,
          });
        }
      } catch {
        // Admission may stay unavailable while an account sheet covers Runtime.
        // Retry the sticky lease on the next claim (page open, reactivation,
        // an Agent turn end or a stream reconnect), without spinning.
        reconcileImmediately = false;
      } finally {
        inFlightLeasesRef.current.delete(key);
        if (reconcileImmediately) {
          window.setTimeout(() => void reconcileRef.current(), 0);
        }
      }
    })();
  }, [leaseKey]);

  const startReceiptDelivery = useCallback((key: string, pending: PendingReceipt) => {
    if (receiptDeliveryByLeaseRef.current.has(key)) return;
    const delivery = (async () => {
      const outcome = await deliverReceipt(pending);
      if (outcome !== 'retry' && pendingReceiptsRef.current.get(key) === pending) {
        pendingReceiptsRef.current.delete(key);
        window.setTimeout(() => void reconcileRef.current(), 0);
      }
    })().finally(() => {
      receiptDeliveryByLeaseRef.current.delete(key);
    });
    receiptDeliveryByLeaseRef.current.set(key, delivery);
  }, []);

  const reconcile = useCallback(async () => {
    if (!enabledRef.current || document.visibilityState !== 'visible') {
      return;
    }
    if (claimMutexRef.current) {
      rerunRequestedRef.current = true;
      return;
    }
    claimMutexRef.current = true;
    rerunRequestedRef.current = false;
    try {
      const identity = await readIdentity();
      if (!identity) return;

      for (const [key, pending] of pendingReceiptsRef.current) {
        if (
          pending.instanceId !== identity.instanceId
          || pending.deviceId !== identity.deviceId
        ) {
          pendingReceiptsRef.current.delete(key);
          continue;
        }
        startReceiptDelivery(key, pending);
      }

      for (let index = 0; index < MAX_CLAIMS_PER_RECONCILE; index += 1) {
        const inFlight = [...inFlightLeasesRef.current.values()];
        const excludeLeaseTokens = [
          ...inFlight.map(({ leaseToken }) => leaseToken),
          ...Array.from(
            pendingReceiptsRef.current.values(),
            (pending) => pending.leaseToken,
          ),
        ];
        const allowCreate = !inFlight.some(({ action }) => action === 'create');
        const claim = await claimNext(
          identity,
          [...new Set(excludeLeaseTokens)],
          allowCreate,
        );
        if (!claim) break;
        startClaim(identity, claim);
      }
    } catch {
      // Opening or returning to the App retries; this path owns no visible error UI.
    } finally {
      claimMutexRef.current = false;
      if (rerunRequestedRef.current) {
        rerunRequestedRef.current = false;
        window.setTimeout(() => void reconcileRef.current(), 0);
      }
    }
  }, [startClaim, startReceiptDelivery]);

  useEffect(() => {
    reconcileRef.current = reconcile;
    enabledRef.current = active;
    // Claims run when the page opens, each time it comes back to the
    // foreground, when an Agent turn ends (the turn that asked for an alarm
    // has queued it by then) and when the event stream (re)connects. Never on
    // a timer. Interim: #3967 replaces these triggers with a push of the
    // request itself.
    void reconcile();
    const stopReactivation = onPageReactivated(() => void reconcile());
    return () => {
      enabledRef.current = false;
      stopReactivation();
    };
  }, [active, reconcile]);

  useEffect(() => {
    if (!active) return undefined;
    return connectWorkbenchEvents({
      onTurnEnd: () => void reconcileRef.current(),
      // The stream replays nothing it missed: catch up on a turn end lost
      // while it was down (the documented use of `onConnected`).
      onConnected: () => void reconcileRef.current(),
    });
  }, [active, connectWorkbenchEvents]);

  return null;
};
