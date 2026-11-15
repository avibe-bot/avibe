(requestId, legacyFallback) => {
  // The same request can be sent by the explicit action and by readiness before
  // either native evaluation callback returns. Only a real listener receipt
  // suppresses the second delivery; an unhandled request remains eligible.
  // A receipt for this or a newer request suppresses a stale queued evaluation.
  // Decimal strings compare without losing precision or requiring BigInt.
  const delivered = window.__AVIBE_DESKTOP_UPDATES_DELIVERED__;
  if (typeof delivered === 'string' && (delivered.length > requestId.length
    || (delivered.length === requestId.length && delivered >= requestId))) return 'handled';
  window.__AVIBE_DESKTOP_UPDATES_HANDLED__ = false;
  window.dispatchEvent(new CustomEvent('avibe:desktop-open-updates', {
    cancelable: true,
    detail: { requestId },
  }));
  if (window.__AVIBE_DESKTOP_UPDATES_HANDLED__) {
    window.__AVIBE_DESKTOP_UPDATES_DELIVERED__ = requestId;
    return 'handled';
  }
  // Readiness replay has no fallback action. Older receivers retain the
  // existing fallback only for the user's explicit attempt.
  if (legacyFallback) window.location.assign('avibe://updates-fallback');
  return 'unhandled';
}
