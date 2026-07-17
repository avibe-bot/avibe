export const UPGRADE_RELOAD_DELAY_MS = 30_000;

type ReloadScheduler = (callback: () => void, delayMs: number) => unknown;

export function scheduleUpgradeReload(
  reload: () => void,
  schedule: ReloadScheduler = (callback, delayMs) => window.setTimeout(callback, delayMs),
): void {
  schedule(reload, UPGRADE_RELOAD_DELAY_MS);
}
