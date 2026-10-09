export type ComputerUseStatus = {
  status: string;
  reason: string | null;
};

export type ComputerUseCopyKey =
  | 'workbench.home.computerUse.ready'
  | 'workbench.home.computerUse.readyDetail'
  | 'workbench.home.computerUse.starting'
  | 'workbench.home.computerUse.startingDetail'
  | 'workbench.home.computerUse.accessibilityDetail'
  | 'workbench.home.computerUse.screenRecordingDetail'
  | 'workbench.home.computerUse.unknownPermissionDetail'
  | 'workbench.home.computerUse.needsPermission'
  | 'workbench.home.computerUse.runtimeTooOldDetail'
  | 'workbench.home.computerUse.runtimeUnavailableDetail'
  | 'workbench.home.computerUse.needsRuntime'
  | 'workbench.home.computerUse.error'
  | 'workbench.home.computerUse.errorDetail'
  | 'workbench.home.computerUse.stateUnwritableDetail'
  | 'workbench.home.computerUse.assetsInvalidDetail'
  | 'workbench.home.computerUse.spawnFailedDetail'
  | 'workbench.home.computerUse.daemonExitedDetail'
  | 'workbench.home.computerUse.healthTimeoutDetail'
  | 'workbench.home.computerUse.bundleIdentityDetail'
  | 'workbench.home.computerUse.axCapabilityDetail'
  | 'workbench.home.computerUse.captureFailedDetail'
  | 'workbench.home.computerUse.driverHealthFailedDetail'
  | 'workbench.home.computerUse.endpointBusyDetail'
  | 'workbench.home.computerUse.endpointUnremovableDetail'
  | 'workbench.home.computerUse.socketUnreachableDetail'
  | 'workbench.home.computerUse.unavailable'
  | 'workbench.home.computerUse.unavailableDetail'
  | 'workbench.home.computerUse.invalidStateFileDetail'
  | 'workbench.home.computerUse.snapshotInvalidDetail'
  | 'workbench.home.computerUse.shellNotRunningDetail'
  | 'workbench.home.computerUse.daemonUnreachableDetail'
  | 'workbench.home.computerUse.off'
  | 'workbench.home.computerUse.offDetail';

export type StatusTone = 'muted' | 'mint' | 'gold' | 'destructive';

const errorDetailKeys: Record<string, ComputerUseCopyKey> = {
  state_unwritable: 'workbench.home.computerUse.stateUnwritableDetail',
  assets_invalid: 'workbench.home.computerUse.assetsInvalidDetail',
  spawn_failed: 'workbench.home.computerUse.spawnFailedDetail',
  daemon_exited: 'workbench.home.computerUse.daemonExitedDetail',
  health_timeout: 'workbench.home.computerUse.healthTimeoutDetail',
  bundle_identity: 'workbench.home.computerUse.bundleIdentityDetail',
  ax_capability: 'workbench.home.computerUse.axCapabilityDetail',
  capture_failed: 'workbench.home.computerUse.captureFailedDetail',
  driver_health_failed: 'workbench.home.computerUse.driverHealthFailedDetail',
  endpoint_busy: 'workbench.home.computerUse.endpointBusyDetail',
  endpoint_unremovable: 'workbench.home.computerUse.endpointUnremovableDetail',
  socket_unreachable: 'workbench.home.computerUse.socketUnreachableDetail',
};

const unavailableDetailKeys: Record<string, ComputerUseCopyKey> = {
  invalid_state_file: 'workbench.home.computerUse.invalidStateFileDetail',
  snapshot_invalid: 'workbench.home.computerUse.snapshotInvalidDetail',
  shell_not_running: 'workbench.home.computerUse.shellNotRunningDetail',
  daemon_unreachable: 'workbench.home.computerUse.daemonUnreachableDetail',
};

export const computerUseStatusCopy = (
  status: ComputerUseStatus,
  t: (key: ComputerUseCopyKey) => string,
): { label: string; detail: string; tone: StatusTone } => {
  if (status.status === 'ready') {
    return { label: t('workbench.home.computerUse.ready'), detail: t('workbench.home.computerUse.readyDetail'), tone: 'mint' };
  }
  if (status.status === 'starting') {
    return { label: t('workbench.home.computerUse.starting'), detail: t('workbench.home.computerUse.startingDetail'), tone: 'gold' };
  }
  if (status.status === 'needs_permission') {
    const detail = status.reason === 'accessibility'
      ? t('workbench.home.computerUse.accessibilityDetail')
      : status.reason === 'screen_recording'
        ? t('workbench.home.computerUse.screenRecordingDetail')
        : t('workbench.home.computerUse.unknownPermissionDetail');
    return { label: t('workbench.home.computerUse.needsPermission'), detail, tone: 'gold' };
  }
  if (status.status === 'needs_runtime') {
    const detail = status.reason === 'runtime_too_old'
      ? t('workbench.home.computerUse.runtimeTooOldDetail')
      : t('workbench.home.computerUse.runtimeUnavailableDetail');
    return { label: t('workbench.home.computerUse.needsRuntime'), detail, tone: 'gold' };
  }
  if (status.status === 'error') {
    const detailKey = errorDetailKeys[status.reason ?? '']
      ?? 'workbench.home.computerUse.errorDetail';
    return { label: t('workbench.home.computerUse.error'), detail: t(detailKey), tone: 'destructive' };
  }
  if (status.status === 'unavailable') {
    const detailKey = unavailableDetailKeys[status.reason ?? '']
      ?? 'workbench.home.computerUse.unavailableDetail';
    return { label: t('workbench.home.computerUse.unavailable'), detail: t(detailKey), tone: 'destructive' };
  }
  return { label: t('workbench.home.computerUse.off'), detail: t('workbench.home.computerUse.offDetail'), tone: 'muted' };
};
