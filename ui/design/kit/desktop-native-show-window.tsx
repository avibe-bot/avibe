// The desktop app's native Show window as the system draws it (ui/design:
// show-pages/desktop-native-entry/*): macOS's own title bar, a stand-in here at the system's
// traffic-light colours (as `AppWindow` draws them), the window's title centred, 28 pt tall, and the
// shipped Show route below it. Its colours and sizes are the `traffic-*` and `titlebar-*` tokens.
//
// The title bar's share button is the shell's (`macos_show_titlebar.rs`, the system
// `square.and.arrow.up` symbol, no label). This stand-in dispatches what the native button
// dispatches, `avibe:show-share-open`, so the shipped `NativeShowShareEntry` opens the shipped share
// popover from the content's top right corner.
import { Share } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Route, Routes } from 'react-router-dom';

import { NATIVE_SHOW_SHARE_OPEN_EVENT } from '@/components/apps/NativeShowShareEntry';
import { StandaloneAppRoute } from '@/components/apps/StandaloneAppRoute';
import { DockProvider } from '@/context/DockProvider';
import { WindowManagerProvider } from '@/context/WindowManagerProvider';

const LIGHTS = ['bg-traffic-close', 'bg-traffic-minimize', 'bg-traffic-zoom'];

function TitleBarShareButton() {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      aria-label={t('chat.showPage.share')}
      onClick={() => window.dispatchEvent(new CustomEvent(NATIVE_SHOW_SHARE_OPEN_EVENT))}
      className="grid size-6 place-items-center rounded-md text-muted"
    >
      <Share className="size-titlebar-symbol" strokeWidth={1.75} />
    </button>
  );
}

/** A native Show window: the system title bar (with the share button unless `today`) and the Show. */
export function NativeShowWindow({ title, today = false }: { title: string; today?: boolean }) {
  return (
    <WindowManagerProvider>
      <DockProvider enabled>
        <div className="flex h-full w-full flex-col overflow-hidden bg-background text-foreground">
          <div className="relative flex h-7 shrink-0 items-center border-b border-border bg-surface px-titlebar-inset">
            <div className="flex items-center gap-2">
              {LIGHTS.map((light) => (
                <span key={light} className={`size-3 rounded-full ${light}`} />
              ))}
            </div>
            <span className="pointer-events-none absolute inset-x-28 truncate text-center text-titlebar font-semibold text-foreground">
              {title}
            </span>
            {today ? null : <div className="ml-auto flex items-center"><TitleBarShareButton /></div>}
          </div>
          <div className="min-h-0 flex-1">
            <Routes>
              <Route path="/apps/show/:sessionId" element={<StandaloneAppRoute appId="showpage" />} />
            </Routes>
          </div>
        </div>
      </DockProvider>
    </WindowManagerProvider>
  );
}
