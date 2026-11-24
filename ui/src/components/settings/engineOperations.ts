// A check or an update of the Model Hub engine, from the request to its outcome. The engine's
// owner answers with a receipt at once and the work is read through the dependency install job
// route. Kept with the Settings page, which is the only caller, so it loads with Settings.
import type { EngineOperationReceipt } from '@/context/ApiContext';

/**
 * What became of one request. `settled`: the receipt finished, and its `status` and `update_state`
 * are the outcome. `refused`: no work began (no controller, a manifest override). `unknown`: the
 * result could not be confirmed (the wait ran out, or the owner no longer has the receipt), which
 * is not evidence that anything failed.
 */
export type EngineOperationResult = EngineOperationReceipt & { outcome: 'settled' | 'refused' | 'unknown' };

type Request = (action: 'check' | 'update' | { job: string }) => Promise<EngineOperationReceipt>;

// Covers a download, the candidate's isolated start, and the wait for running work to finish.
const WAIT_MS = 430_000;
const POLL_MS = 1_000;

const done = (receipt: EngineOperationReceipt | null | undefined) =>
  receipt?.status === 'succeeded' || receipt?.status === 'failed';

/**
 * Ask for a check or an update and wait for its result. Nothing is ever sent twice: a refusal is
 * returned as one, and a result that cannot be confirmed is reported as unknown, for the page to
 * read the row again.
 */
export async function runEngineOperation(request: Request, kind: 'check' | 'update'): Promise<EngineOperationResult> {
  const deadline = Date.now() + WAIT_MS;
  const settle = async (job: string, first: EngineOperationReceipt): Promise<EngineOperationResult> => {
    let last = first;
    while (!done(last) && Date.now() < deadline) {
      await new Promise((resolve) => { window.setTimeout(resolve, POLL_MS); });
      last = await request({ job });
      // A receipt the owner no longer has will never finish.
      if (last?.status === undefined) break;
    }
    return { ...last, outcome: done(last) ? 'settled' : 'unknown' };
  };
  const started = await request(kind);
  if (typeof started?.job_id === 'string') return settle(started.job_id, started);
  // An update asked for while a check runs is refused with that check's id. Wait for the check,
  // so the row is read after it; whether to update is then the user's to ask again.
  if (started?.error === 'operation_busy' && typeof started.operation_id === 'string') {
    return settle(started.operation_id, { ok: true, kind: 'check', status: 'running' });
  }
  return { ...started, ok: false, outcome: 'refused' };
}
