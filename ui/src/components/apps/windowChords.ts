// Focus-surface predicates shared by the window keyboard chords (WindowLayer) and
// the Show Page iframe ⌥W bridge (ShowPageApp). Kept in a leaf module so a
// lazy-loaded app body can reuse them without importing the WindowLayer component
// (which would create an import cycle through the app registry).

// In the TERMINAL, Ctrl is a control-character stream — ^W deletes a word, ^M is
// carriage return — so a window chord must never hijack Ctrl there (xterm focuses a
// hidden textarea inside its `.xterm` root). The editor is the opposite: Monaco has no
// useful Ctrl+W, so we WANT Ctrl+W to close its window (guarded for unsaved edits)
// rather than be swallowed and bypass the prompt — hence the exemption is terminal-only.
export function inTerminalSurface(el: Element | null): boolean {
  return el instanceof HTMLElement && !!el.closest('.xterm');
}

export function inTextEntrySurface(el: Element | null): boolean {
  return (
    el instanceof HTMLElement &&
    !!el.closest(
      'input, textarea, select, [contenteditable="true"], [role="textbox"], .monaco-editor, .xterm',
    )
  );
}
