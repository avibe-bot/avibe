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
  | 'workbench.home.computerUse.unavailable'
  | 'workbench.home.computerUse.unavailableDetail'
  | 'workbench.home.computerUse.off'
  | 'workbench.home.computerUse.offDetail';

export type StatusTone = 'muted' | 'mint' | 'gold' | 'destructive';

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
    return { label: t('workbench.home.computerUse.error'), detail: t('workbench.home.computerUse.errorDetail'), tone: 'destructive' };
  }
  if (status.status === 'unavailable') {
    return { label: t('workbench.home.computerUse.unavailable'), detail: t('workbench.home.computerUse.unavailableDetail'), tone: 'destructive' };
  }
  return { label: t('workbench.home.computerUse.off'), detail: t('workbench.home.computerUse.offDetail'), tone: 'muted' };
};
