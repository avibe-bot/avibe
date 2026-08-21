export type AppWindowExitKind = 'close' | null;

/**
 * Which motion class a window root should carry.
 *
 * A window that mounted already minimized (reload of a Dock-hidden window)
 * must not play `animate-appwindow-in`: that keyframe animates `opacity` to 1
 * and wins over the minimized `opacity-0` class, so the window flashes at
 * full size before disappearing. Capture skipEntrance at first paint and keep
 * it for the instance's life — restoring from the Dock uses the CSS
 * transform/opacity transition, and toggling the in-class on restore would
 * re-trigger the keyframe on top of that morph.
 */
export function appWindowMotionClass(opts: {
  exitKind: AppWindowExitKind;
  skipEntrance: boolean;
}): string | undefined {
  if (opts.exitKind === 'close') return 'animate-appwindow-out';
  if (opts.skipEntrance) return undefined;
  return 'animate-appwindow-in';
}
