import { apiFetch } from '../../lib/apiFetch';
import type { ComputerUseStatus, StatusTone } from './computerUseStatusCopy';

export const COMPUTER_USE_STATUS_PATH = '/api/desktop/computer-use/status';
export const COMPUTER_USE_SETTINGS_PATH = '/settings/computer-use';
export const COMPUTER_USE_REFRESH_INTERVAL_MS = 5_000;
/** The setup guide polls faster: a grant should show up as soon as macOS records it. */
export const COMPUTER_USE_GUIDE_REFRESH_INTERVAL_MS = 1_500;

/**
 * One read of the desktop status endpoint.
 *
 * - `unsupported`: the host has no live desktop shell (`supported: false`).
 * - `null`: a transient or malformed response. It is not a Computer Use state,
 *   so callers keep whatever they last observed.
 */
export type ComputerUseStatusRead =
  | { kind: 'unsupported' }
  | { kind: 'status'; status: ComputerUseStatus };

export const readComputerUseStatus = async (): Promise<ComputerUseStatusRead | null> => {
  try {
    const response = await apiFetch(COMPUTER_USE_STATUS_PATH, { cache: 'no-store' });
    if (!response.ok) return null;
    const payload = await response.json() as Partial<ComputerUseStatus> & { supported?: boolean };
    if (payload.supported === false) return { kind: 'unsupported' };
    if (typeof payload.status !== 'string') return null;
    return {
      kind: 'status',
      status: {
        status: payload.status,
        reason: typeof payload.reason === 'string' ? payload.reason : null,
      },
    };
  } catch {
    return null;
  }
};

export const computerUseToneClass: Record<StatusTone, string> = {
  muted: 'border-border bg-surface text-muted',
  mint: 'border-mint/30 bg-mint-soft text-mint-ink',
  gold: 'border-gold/35 bg-gold/[0.08] text-gold-ink',
  destructive: 'border-destructive/35 bg-destructive/[0.06] text-destructive-ink',
};

export const COMPUTER_USE_GUIDE_STEPS = [
  'enable',
  'accessibility',
  'screenRecording',
] as const;

export type ComputerUseGuideStep = (typeof COMPUTER_USE_GUIDE_STEPS)[number];
export type ComputerUseGuideStepState = 'complete' | 'current' | 'pending';

/**
 * How far the setup has observably progressed.
 *
 * Steps only claim progress the status reports. The managed driver checks
 * Accessibility before Screen Recording, so `screen_recording` proves
 * Accessibility passed while `accessibility` says nothing about Screen
 * Recording. Every other non-ready state (starting, waiting for the service,
 * error) only proves Computer Use was turned on; the status header carries
 * the detail for those.
 */
export const computerUseGuideProgress = (
  status: ComputerUseStatus,
): Record<ComputerUseGuideStep, ComputerUseGuideStepState> => {
  const stateAt = (completed: number, current: number | null) =>
    Object.fromEntries(COMPUTER_USE_GUIDE_STEPS.map((step, index) => [
      step,
      index < completed ? 'complete' : index === current ? 'current' : 'pending',
    ])) as Record<ComputerUseGuideStep, ComputerUseGuideStepState>;

  switch (status.status) {
    case 'ready':
      return stateAt(COMPUTER_USE_GUIDE_STEPS.length, null);
    case 'needs_permission':
      return status.reason === 'screen_recording' ? stateAt(2, 2) : stateAt(1, 1);
    case 'starting':
    case 'needs_runtime':
    case 'error':
      return stateAt(1, null);
    case 'unavailable':
      // The shell or driver cannot be read, so even the toggle is unknown.
      return stateAt(0, null);
    default:
      // `off`, and any state this build does not know, which the status copy
      // also renders as off.
      return stateAt(0, 0);
  }
};
