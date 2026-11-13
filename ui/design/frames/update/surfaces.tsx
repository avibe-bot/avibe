// The three places one computer's update is read, drawn side by side for update/panel.
import type { ReactNode } from 'react';

import type { VersionInfo } from '@/context/ApiContext';
import type { DesktopUpdateState } from '@/lib/desktopShell';
import { UpdatePanel, type UpdatePanelActions } from '@/components/update/UpdatePanel';
import {
  modelFromDesktopRelay,
  modelFromDesktopState,
  type UpdatePanelModel,
} from '@/components/update/updatePanelModel';

import { useDraft } from '../../kit/draft';

export type SurfaceState = {
  /** The desktop app's own updater, or none where only an older app or no app reports. */
  desktop?: DesktopUpdateState;
  /** The desktop app's own version, when it has already moved on. */
  desktopVersion?: string;
  web: VersionInfo;
};

const CURRENT = '3.2.6';
const quiet = { requesting: false, error: null, timedOut: false, disconnected: false };

const desktopActions = (model: UpdatePanelModel): UpdatePanelActions => ({
  check: { run: () => {} },
  ...(model.status?.kind === 'available' ? { update: { run: () => {} } } : {}),
  close: () => {},
});

const webActions = (model: UpdatePanelModel): UpdatePanelActions => ({
  check: { run: () => {}, refreshOnly: true },
  ...(model.status?.kind === 'available' || (model.status?.kind === 'update-failed' && model.status.retry)
    ? { update: { run: () => {} } } : {}),
  close: () => {},
});

function Popover({ children }: { children: ReactNode }) {
  return (
    <div className="w-80 overflow-hidden rounded-xl border border-edge bg-popover shadow-elevation-2">{children}</div>
  );
}

function Column({ label, where, children }: { label: string; where: string; children: ReactNode }) {
  return (
    <div className="flex w-80 flex-col gap-3">
      <div>
        <p className="text-sm font-semibold text-foreground">{label}</p>
        <p className="text-xs text-muted">{where}</p>
      </div>
      {children}
    </div>
  );
}

export function Surfaces({ state }: { state: SurfaceState }) {
  const d = useDraft();
  const web = modelFromDesktopRelay(state.web, quiet);
  return (
    <div className="flex h-full items-start justify-center gap-8 bg-background p-8 text-foreground">
      <Column label={d('桌面应用', 'Desktop app')} where={d('侧栏底部版本号', 'Version in the sidebar footer')}>
        {state.desktop ? (
          <Popover>
            {(() => {
              const model = modelFromDesktopState(state.desktop, { current: state.desktopVersion ?? CURRENT, checkedAt: state.desktop.checkedAt });
              return <UpdatePanel model={model} actions={desktopActions(model)} />;
            })()}
          </Popover>
        ) : (
          <p className="rounded-xl border border-dashed border-border-strong p-4 text-xs leading-5 text-muted">
            {d('较旧的桌面应用不提供这块面板，用它自己的更新窗口。', 'An older desktop app has no panel here; it uses its own update window.')}
          </p>
        )}
      </Column>
      <Column label={d('本机 Web', 'Local Web')} where={d('浏览器打开本机 Avibe，侧栏版本号', 'Avibe in a browser on the same computer')}>
        <Popover><UpdatePanel model={web} actions={webActions(web)} /></Popover>
      </Column>
      <Column label={d('远程 Web', 'Remote Web')} where={d('通过 avibe.bot 打开那台电脑', 'The same computer through avibe.bot')}>
        <Popover><UpdatePanel model={web} actions={webActions(web)} /></Popover>
      </Column>
    </div>
  );
}

export function Phone({ state }: { state: SurfaceState }) {
  const web = modelFromDesktopRelay(state.web, quiet);
  return (
    <div className="relative h-full bg-background">
      <div className="absolute inset-0 bg-foreground/10" aria-hidden="true" />
      <div className="absolute inset-x-3 top-[calc(4.5rem+env(safe-area-inset-top))] overflow-hidden rounded-xl border border-edge bg-popover shadow-elevation-2">
        <UpdatePanel model={web} actions={webActions(web)} />
      </div>
    </div>
  );
}
