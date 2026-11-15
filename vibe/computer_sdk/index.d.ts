export type EffectStatus =
  | "confirmed"
  | "partial"
  | "unverifiable"
  | "suspected_noop"
  | "refused";

export interface Effect {
  status: EffectStatus;
  effect: EffectStatus;
  route?: string;
  summary?: string | null;
  delivery?: {
    mode: "background" | "foreground" | "not_applicable" | "unknown";
    delivered_count?: number | null;
  } | null;
  evidence?: Array<{ kind: string; detail?: string | null }> | null;
  escalation?: { target: string; reason: string } | null;
  error?: { code: string; hint?: string | null } | null;
  [key: string]: unknown;
}

export interface WaitOptions {
  timeout?: number;
}

export interface BaseSelector {
  role?: string;
  label?: string;
  labelContains?: string;
  value?: string | null;
  index?: number;
}

export interface Selector extends BaseSelector {
  within?: Selector;
}

export interface AppSelector extends BaseSelector {
  path?: never;
  bundleId?: string;
  name?: string;
  nameContains?: string;
}

/** Windows-only explicit executable target. No shell expansion or app-name lookup. */
export interface PathAppSelector {
  path: string;
  bundleId?: never;
  name?: never;
  nameContains?: never;
  role?: never;
  label?: never;
  labelContains?: never;
  value?: never;
  index?: never;
}

/** Literal argv and startup presentation; the executable is fixed by the handle. */
export interface PathLaunchOptions {
  additional_arguments?: string[];
  start_minimized?: boolean;
  path?: never;
  name?: never;
  bundle_id?: never;
  launch_path?: never;
  aumid?: never;
  urls?: never;
  reuse?: never;
  creates_new_application_instance?: never;
}

export interface WindowSelector extends BaseSelector {
  title?: string;
  titleContains?: string;
  /** Exact driver window id, as listed by `vibe computer observe windows`. */
  windowId?: number;
  /** Match only windows whose on-screen state equals this value. */
  onScreen?: boolean;
}

export interface ActionOptions {
  /**
   * Driver delivery rung. `"background"` (default) leaves focus alone;
   * `"foreground"` briefly fronts the exact window, acts, then restores the
   * previous frontmost window.
   */
  delivery_mode?: "background" | "foreground";
  [key: string]: unknown;
}

export interface ElementExpectation {
  toExist(options?: WaitOptions | number): Promise<void>;
  toHaveValue(
    value: string | null,
    options?: WaitOptions | number,
  ): Promise<void>;
  toHaveLabel(label: string, options?: WaitOptions | number): Promise<void>;
  toBeEnabled(options?: WaitOptions | number): Promise<void>;
  toBeSelected(options?: WaitOptions | number): Promise<void>;
}

export interface ElementHandle {
  expect(): ElementExpectation;
  click(options?: ActionOptions): Promise<Effect>;
  doubleClick(options?: ActionOptions): Promise<Effect>;
  rightClick(options?: ActionOptions): Promise<Effect>;
  fill(value: string): Promise<Effect>;
  type(text: string, options?: ActionOptions): Promise<Effect>;
  press(key: string, options?: ActionOptions): Promise<Effect>;
  hotkey(keys: string[], options?: ActionOptions): Promise<Effect>;
  scroll(direction: string, options?: ActionOptions): Promise<Effect>;
}

export interface WindowHandle {
  readonly pid: number;
  readonly windowId: number;
  readonly title: string;
  element(selector: Selector): ElementHandle;
  expect(selector: Selector): ElementExpectation;
  waitFor(
    selector: Selector,
    options?: WaitOptions | number,
  ): Promise<ElementHandle>;
  observe(options?: ActionOptions): Promise<Record<string, unknown>>;
  /** Bring this exact window to the front and leave it there. */
  activate(): Promise<Effect>;
  click(options: ActionOptions): Promise<Effect>;
  type(text: string, options?: ActionOptions): Promise<Effect>;
  press(key: string, options?: ActionOptions): Promise<Effect>;
  hotkey(keys: string[], options?: ActionOptions): Promise<Effect>;
  scroll(direction: string, options?: ActionOptions): Promise<Effect>;
  drag(options: ActionOptions): Promise<Effect>;
}

export interface AppHandle {
  readonly pid: number | null;
  readonly name: string;
  readonly bundleId: string | null;
  launch(options?: ActionOptions & { path?: never }): Promise<LaunchEffect>;
  forceQuit(): Promise<Effect>;
  window(selector?: WindowSelector): Promise<WindowHandle>;
  waitFor(
    selector?: WindowSelector,
    options?: WaitOptions | number,
  ): Promise<WindowHandle>;
}

/**
 * Windows launch-only executable target. launch() always creates a new process;
 * it returns an ordinary app handle only after one existing metadata lookup finds
 * the original created PID uniquely live. This is not image identity proof or
 * reuse. Existing applications use the name/bundle surface.
 */
export interface PathAppHandle {
  readonly path: string;
  launch(options?: PathLaunchOptions): Promise<LaunchEffect>;
}

export interface LaunchEffect extends Effect {
  app: AppHandle;
}

export interface Computer {
  readonly clipboard: {
    read(options?: ActionOptions): Promise<Record<string, unknown>>;
    write(text: string): Promise<Effect>;
  };
  /** Select a Windows launch target; this does not attach by name or launch hint. */
  app(selector: PathAppSelector): Promise<PathAppHandle>;
  app(selector?: AppSelector): Promise<AppHandle>;
  waitFor(
    selector?: AppSelector,
    options?: WaitOptions | number,
  ): Promise<AppHandle>;
}

export class ControllerError extends Error {
  readonly code: string;
  readonly params: Record<string, unknown>;
  readonly ambiguous: boolean;
  readonly step?: string;
}

export class NotFound extends ControllerError {}
export class Ambiguous extends ControllerError {}
export class Timeout extends ControllerError {}
export class TargetBusy extends ControllerError {}
/** @deprecated Use TargetBusy. */
export type DesktopBusy = TargetBusy;
/** @deprecated Use TargetBusy. */
export const DesktopBusy: typeof TargetBusy;
/** @deprecated Foreground input needs no consent; this is never thrown. */
export class ForegroundConsentRequired extends ControllerError {}
export class Unavailable extends ControllerError {}
export class ConfirmRefused extends ControllerError {}

export const computer: Computer;

export interface Avibe {
  notify(message: string): Promise<Effect>;
  confirm(message: string): Promise<Effect>;
}

export const avibe: Avibe;
