import { useEffect, useSyncExternalStore } from 'react';
import type { FC } from 'react';
import { CircleAlert, CircleCheck, LoaderCircle, ShieldAlert } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { isDesktopShell } from '../../lib/desktopShell';
import {
  COMPUTER_USE_REFRESH_INTERVAL_MS,
  COMPUTER_USE_SETTINGS_PATH,
  computerUseToneClass,
  readComputerUseStatus,
} from './computerUseStatus';
import {
  computerUseStatusCopy,
  type ComputerUseCopyKey,
  type ComputerUseStatus,
} from './computerUseStatusCopy';

let statusSnapshot: ComputerUseStatus | null = null;
const listeners = new Set<() => void>();

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) statusSnapshot = null;
  };
};

const getStatusSnapshot = () => statusSnapshot;

const publishStatus = (next: ComputerUseStatus | null) => {
  if (
    statusSnapshot?.status === next?.status
    && statusSnapshot?.reason === next?.reason
  ) {
    return;
  }
  statusSnapshot = next;
  for (const listener of listeners) listener();
};

const refreshComputerUseStatus = async () => {
  const read = await readComputerUseStatus();
  // A transient UI response is not a Computer Use state. Keep the last
  // observed line until the next no-store refresh.
  if (!read) return;
  publishStatus(read.kind === 'status' ? read.status : null);
};

export const ComputerUseStatusIcon: FC<{ status: string }> = ({ status }) => {
  if (status === 'ready') return <CircleCheck className="size-4 shrink-0" aria-hidden="true" />;
  if (status === 'needs_permission' || status === 'needs_runtime') {
    return <ShieldAlert className="size-4 shrink-0" aria-hidden="true" />;
  }
  if (status === 'starting') return <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" />;
  if (status === 'error' || status === 'unavailable') {
    return <CircleAlert className="size-4 shrink-0" aria-hidden="true" />;
  }
  return <CircleCheck className="size-4 shrink-0" aria-hidden="true" />;
};

export const ComputerUseStatusLine: FC = () => {
  const { t } = useTranslation();
  const status = useSyncExternalStore(subscribe, getStatusSnapshot, getStatusSnapshot);

  useEffect(() => {
    void refreshComputerUseStatus();
    const interval = window.setInterval(() => void refreshComputerUseStatus(), COMPUTER_USE_REFRESH_INTERVAL_MS);
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') void refreshComputerUseStatus();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    const onFocus = () => {
      void refreshComputerUseStatus();
    };
    window.addEventListener('focus', onFocus);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onFocus);
    };
  }, []);

  if (!status) return null;
  const copy = computerUseStatusCopy(status, (key: ComputerUseCopyKey) => t(key));
  const setupLink = status.status !== 'ready' && isDesktopShell();
  return (
    <div
      aria-label={copy.label}
      className={`flex w-full items-start gap-2.5 rounded-[10px] border px-4 py-2.5 text-[12px] ${computerUseToneClass[copy.tone]}`}
      data-computer-use-status={status.status}
      data-testid="computer-use-status"
    >
      <ComputerUseStatusIcon status={status.status} />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{copy.label}</p>
        <p className="mt-0.5 text-[11px] opacity-80">{copy.detail}</p>
      </div>
      {/* The step-by-step guide lives in Settings, which only the desktop
          shell shows; a browser tab keeps the line as status alone. */}
      {setupLink && (
        <Link
          to={COMPUTER_USE_SETTINGS_PATH}
          className="shrink-0 self-center rounded-md px-2 py-1 text-[11px] font-semibold underline-offset-2 hover:underline"
        >
          {t('workbench.home.computerUse.finishInSettings')}
        </Link>
      )}
    </div>
  );
};
