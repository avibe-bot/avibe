// Proposed: the native Show window's title bar carrying every control the Workbench's Show window
// carries (ui/design: show-pages/desktop-native-controls/*). Today it carries only the share button
// (`kit/desktop-native-show-window.tsx`, show-pages/desktop-native-entry/titlebar-idle).
//
// The window frame is AppKit's. This stand-in draws it as that kit does — traffic lights at the
// system's colours, the window's title, 28 pt tall, the shipped Show route below — and widens its
// single-button title-bar accessory into one group.
//
// In the shipped build each button is an NSButton the shell draws in that accessory
// (`macos_show_titlebar.rs`): a 14 pt medium SF symbol at label colour, pressed anywhere in a 28 pt
// square, its hover wash a 20 pt square inside that; the buttons 4 pt apart and the group 9 pt from
// the window's right edge — the same inset the traffic lights take on the left. The share button's
// own symbol is 15 pt today and joins this group, so it drops to 14 with the rest.
// The symbols stand for the Workbench window's icons:
//
//   plus.bubble (标注) · message (打开聊天) · square.and.arrow.up (分享) · arrow.up.forward.square (在浏览器打开)
//
// Here they are the lucide icons the Workbench title bar itself uses, at the native size, so the
// frame shows the group's rhythm and where each popover opens. The annotate popover and the share
// popover are the shipped components, anchored in their own button's cell: the shipped
// `NativeShowShareEntry`'s single anchor at the content's top right corner becomes a row of
// anchors mirroring the group, so a popover opens under the button that asked for it.
import { useRef, type ReactNode } from 'react';
import { ChevronRight, ExternalLink, MessageCircle, MessageSquarePlus, MoreHorizontal, Share } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Route, Routes } from 'react-router-dom';

import { StandaloneAppRoute } from '@/components/apps/StandaloneAppRoute';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { ShowPageAnnotateControl } from '@/components/workbench/ShowPageAnnotateControl';
import { ShowPageShareControl } from '@/components/workbench/ShowPageShareControl';
import type { AnnotationState } from '@/components/workbench/useShowPageAnnotation';
import { DockProvider } from '@/context/DockProvider';
import { WindowManagerProvider } from '@/context/WindowManagerProvider';

const LIGHTS = ['bg-traffic-close', 'bg-traffic-minimize', 'bg-traffic-zoom'];

/** A title-bar button the shell draws natively. The press target stays the 28 pt square the group's
 *  geometry is built on; the hover wash is a 20 pt square inside it, so it keeps 4 pt clear of the
 *  title bar's top and bottom and 8 pt of its neighbours (Max, 2026-11-21 09:36). */
const BUTTON = 'group/ctrl grid size-7 place-items-center text-muted transition-colors hover:text-foreground';
const WASH = 'grid size-5 place-items-center rounded-md transition-colors group-hover/ctrl:bg-wash';
/** The symbol, 14 pt (Max, 2026-11-21 09:47). The shipped share button's own symbol is 15 pt
 *  (`--spacing-titlebar-symbol`, `macos_show_titlebar.rs`'s `SYMBOL`); it joins this group, so
 *  implementing this design drops both to 14. */
const SYMBOL = 'size-3.5';
/** The shipped annotate control's own trigger, brought to the same wash and symbol as the stand-ins. */
const ANNOTATE = 'grid size-7 place-items-center [&_button]:size-5 [&_button]:rounded-md [&_svg]:size-3.5';

const noop = () => {};

/** The session-chat button: it raises the Workbench window on this Show's chat. */
function ChatButton() {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      data-titlebar-control="chat"
      aria-label={t('apps.window.openChat')}
      title={t('apps.window.openChat')}
      className={BUTTON}
    >
      <span className={WASH}>
        <MessageCircle className={SYMBOL} strokeWidth={1.75} />
      </span>
    </button>
  );
}

/** The open-in-browser button: the same Show, in the system's browser. */
function BrowserButton({ label }: { label: string }) {
  return (
    <button type="button" data-titlebar-control="browser" aria-label={label} title={label} className={BUTTON}>
      <span className={WASH}>
        <ExternalLink className={SYMBOL} strokeWidth={1.75} />
      </span>
    </button>
  );
}

/** The share button: the native symbol, opening the shipped share popover under itself. */
function ShareButton({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const cell = useRef<HTMLDivElement>(null);
  return (
    <div ref={cell} className="relative grid size-7 place-items-center">
      <div className="pointer-events-none absolute inset-0 grid place-items-center">
        <ShowPageShareControl compact anchorOnly sessionId={sessionId} canManageInstance canPinToDock />
      </div>
      <button
        type="button"
        aria-label={t('chat.showPage.share')}
        title={t('chat.showPage.share')}
        onClick={() => cell.current?.querySelector<HTMLButtonElement>('button[aria-hidden="true"]')?.click()}
        className={`relative ${BUTTON}`}
      >
        <span className={WASH}>
          <Share className={SYMBOL} strokeWidth={1.75} />
        </span>
      </button>
    </div>
  );
}

/** Option B's overflow button and its menu: the three controls the title bar does not draw. */
function OverflowButton({ browserLabel }: { browserLabel: string }) {
  const { t } = useTranslation();
  const row = 'flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-titlebar text-foreground transition-colors hover:bg-wash';
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={t('nav.more')} title={t('nav.more')} className={BUTTON}>
          <span className={WASH}>
            <MoreHorizontal className={SYMBOL} strokeWidth={1.75} />
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" sideOffset={6} className="w-56 p-1.5">
        <button type="button" className={row}>
          <MessageSquarePlus className="size-4 shrink-0 text-muted" />
          <span className="min-w-0 flex-1 truncate">{t('chat.showPage.annotate.toggle')}</span>
          <ChevronRight className="size-4 shrink-0 text-muted" />
        </button>
        <button type="button" className={row}>
          <MessageCircle className="size-4 shrink-0 text-muted" />
          <span className="min-w-0 flex-1 truncate">{t('apps.window.openChat')}</span>
        </button>
        <button type="button" className={row}>
          <ExternalLink className="size-4 shrink-0 text-muted" />
          <span className="min-w-0 flex-1 truncate">{browserLabel}</span>
        </button>
      </PopoverContent>
    </Popover>
  );
}

export interface NativeShowControlsProps {
  /** The window's title, which the page gives it. */
  title: string;
  /** The Show this window was opened for. */
  sessionId: string;
  /** Proposed copy for `apps.window.openInBrowser`, which the Workbench's "open in new tab" label
   *  cannot stand in for: a native window has no tabs. The frame passes it in its own language. */
  browserLabel: string;
  /** A: one group of four, as the Workbench window orders them. B: share, and the rest behind `•••`. */
  variant?: 'group' | 'overflow';
  /** The annotation overlay's state, which the title bar's annotate button draws. */
  annotation?: AnnotationState | null;
  /** How the title is laid out: `system` is macOS 25 and earlier, which centres it; `drawn` is
   *  macOS 26 and later, where the shell draws it from the traffic lights and truncates it before
   *  the group. */
  titleDraw?: 'system' | 'drawn';
}

/** A native Show window with the proposed title-bar controls, and the shipped Show below them. */
export function NativeShowControlsWindow({
  title,
  sessionId,
  browserLabel,
  variant = 'group',
  annotation = null,
  titleDraw = 'system',
}: NativeShowControlsProps) {
  const controls: ReactNode = variant === 'group' ? (
    <>
      <div className={ANNOTATE}>
        <ShowPageAnnotateControl compact state={annotation} onEnable={noop} onDisable={noop} onSetMode={noop} />
      </div>
      <ChatButton />
      <ShareButton sessionId={sessionId} />
      <BrowserButton label={browserLabel} />
    </>
  ) : (
    <>
      <OverflowButton browserLabel={browserLabel} />
      <ShareButton sessionId={sessionId} />
    </>
  );
  return (
    <WindowManagerProvider>
      <DockProvider enabled>
        <div className="flex h-full w-full flex-col overflow-hidden bg-background text-foreground">
          <div className="relative flex h-7 shrink-0 items-center gap-2 border-b border-border bg-surface px-titlebar-inset">
            <div className="flex shrink-0 items-center gap-2">
              {LIGHTS.map((light) => (
                <span key={light} className={`size-3 rounded-full ${light}`} />
              ))}
            </div>
            {titleDraw === 'drawn' ? (
              <span className="min-w-0 flex-1 truncate text-titlebar font-semibold text-foreground">{title}</span>
            ) : (
              <span className="pointer-events-none absolute inset-x-36 truncate text-center text-titlebar font-semibold text-foreground">
                {title}
              </span>
            )}
            <div className="ml-auto flex shrink-0 items-center gap-1">{controls}</div>
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
