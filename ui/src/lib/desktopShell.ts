declare global {
  interface Window {
    /** Defined by the Avibe desktop shell before any page script runs, top-level document only. */
    readonly __AVIBE_DESKTOP_SHELL__?: true;
    readonly __AVIBE_DESKTOP_VERSION__?: string;
  }
}

/**
 * True when this document is the Avibe desktop shell's own window rather than a
 * browser tab or an installed PWA. This is the host, not the viewport size: see
 * `useIsDesktop` for the layout breakpoint.
 *
 * The shell draws the window's title bar and owns the native Settings… menu, so
 * the page leaves out the top bars a browser tab needs for the same job.
 */
export function isDesktopShell(): boolean {
  return typeof window !== 'undefined' && window.__AVIBE_DESKTOP_SHELL__ === true;
}

/**
 * The native Settings… item (⌘,) dispatches this cancelable event on `window`.
 * A listener that opens Settings in place cancels it; left uncancelled, the
 * shell loads Settings as a page instead.
 */
export const DESKTOP_OPEN_SETTINGS_EVENT = 'avibe:desktop-open-settings';

/**
 * Marks an element the desktop shell's title bar extends over: a press on it
 * that no control claims moves the window, as the band under the traffic lights
 * does. For a page's own top bar, such as setup's brand row.
 */
const SHELL_TITLE_BAR_ATTR = 'data-shell-title-bar';

/** The shell's WebKit message handler (`desktop/src-tauri/src/macos_title_bar.rs`). */
const TITLE_BAR_MESSAGES = 'avibeShellTitleBar';
type TitleBarRequest = 'drag' | 'double-click';

type WebKitMessageHandlers = Record<string, { postMessage(body: TitleBarRequest): void } | undefined>;

/**
 * ARIA roles whose element, padding included, belongs to the person rather than
 * to the window: every interactive widget, every composite that holds them (an
 * open listbox's frame is part of the menu, not free title bar), and the
 * dialogs popups render as.
 */
const INTERACTIVE_ROLES = [
  'button', 'checkbox', 'gridcell', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'radio',
  'scrollbar', 'searchbox', 'separator', 'slider', 'spinbutton', 'switch', 'tab', 'tabpanel', 'textbox', 'treeitem',
  'combobox', 'grid', 'listbox', 'menu', 'menubar', 'radiogroup', 'tablist', 'tree', 'treegrid',
  'dialog', 'alertdialog',
];

/** Whatever a press belongs to rather than to the window: the same set a native title bar leaves alone. */
const CONTROL_SELECTOR = [
  'a', 'button', 'input', 'select', 'textarea', 'label', 'summary', 'details', 'dialog', 'iframe',
  '[contenteditable]:not([contenteditable="false"])',
  '[tabindex]:not([tabindex="-1"])',
  '[draggable="true"]',
  ...INTERACTIVE_ROLES.map((role) => `[role="${role}"]`),
].join(', ');

/**
 * How far, in CSS pixels, the pointer may drift between a double-click's press
 * and release and still count as one: hand jitter, not a drag. AppKit starts a
 * drag at about the same distance.
 */
const DOUBLE_CLICK_SLOP = 4;

function titleBarHandler(): WebKitMessageHandlers[string] {
  const webkit = (window as Window & { webkit?: { messageHandlers?: WebKitMessageHandlers } }).webkit;
  return webkit?.messageHandlers?.[TITLE_BAR_MESSAGES];
}

/** Height of the overlay title bar the shell publishes; `0px` under a standard title bar or outside the shell. */
function titleBarInset(): number {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--shell-titlebar-inset')) || 0;
}

function isTitleBarPress(event: MouseEvent): boolean {
  if (event.button !== 0 || event.defaultPrevented || !(event.target instanceof Element)) return false;
  if (event.target.closest(CONTROL_SELECTOR)) return false;
  // Under a standard title bar (the shell rejected this page's geometry) the
  // system title bar moves the window and the page is only content: no band and
  // no marked region.
  const inset = titleBarInset();
  if (inset <= 0) return false;
  return event.clientY < inset || event.target.closest(`[${SHELL_TITLE_BAR_ATTR}]`) !== null;
}

/**
 * Lets the whole title bar move the window, not only the native strip over the
 * sidebar's top. The shell can place its strip only where it knows the page keeps
 * no controls; the page knows where they are, so a press on the overlay band — or
 * on an element marked {@link SHELL_TITLE_BAR_ATTR} — that no control claims asks
 * the shell to start the drag. A double-click that barely moved does what the
 * system title bar's double-click does, released on mouseup as macOS decides it.
 * Under a shell without the handler this installs nothing and changes nothing.
 */
export function installDesktopTitleBar(): () => void {
  if (!isDesktopShell() || !titleBarHandler()) return () => {};
  let doubleClickAt: { x: number; y: number } | null = null;
  const onMouseDown = (event: MouseEvent) => {
    doubleClickAt = null;
    if (!isTitleBarPress(event)) return;
    if (event.detail === 2) {
      doubleClickAt = { x: event.clientX, y: event.clientY };
      return;
    }
    if (event.detail !== 1) return;
    // No text cursor and no focus change: this press belongs to the window.
    event.preventDefault();
    titleBarHandler()?.postMessage('drag');
  };
  const onMouseUp = (event: MouseEvent) => {
    const origin = doubleClickAt;
    doubleClickAt = null;
    if (!origin || event.detail !== 2) return;
    if (Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > DOUBLE_CLICK_SLOP) return;
    if (isTitleBarPress(event)) titleBarHandler()?.postMessage('double-click');
  };
  document.addEventListener('mousedown', onMouseDown);
  document.addEventListener('mouseup', onMouseUp);
  return () => {
    document.removeEventListener('mousedown', onMouseDown);
    document.removeEventListener('mouseup', onMouseUp);
  };
}
