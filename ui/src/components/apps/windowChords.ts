// Focus-surface predicates shared by the window keyboard chords (WindowLayer) and
// the Show Page iframe ⌥W bridge (ShowPageApp). Kept in a leaf module so a
// lazy-loaded app body can reuse them without importing the WindowLayer component
// (which would create an import cycle through the app registry).
//
// Realm-agnostic by design: the ⌥W bridge passes `iframe.contentDocument.activeElement`
// from a same-origin Show Page iframe, whose elements live in a DIFFERENT window/realm.
// `instanceof HTMLElement` is false across realms, so we duck-type on `closest` (present
// on every Element in any realm) instead — otherwise the input/editor/terminal exemption
// would never fire inside a Show Page and ⌥W would close the window mid-typing (Codex).

// In the TERMINAL, Ctrl is a control-character stream — ^W deletes a word, ^M is
// carriage return — so a window chord must never hijack Ctrl there (xterm focuses a
// hidden textarea inside its `.xterm` root). The editor is the opposite: Monaco has no
// useful Ctrl+W, so we WANT Ctrl+W to close its window (guarded for unsaved edits)
// rather than be swallowed and bypass the prompt — hence the exemption is terminal-only.
export function inTerminalSurface(el: Element | null): boolean {
  return !!el?.closest?.('.xterm');
}

export function inTextEntrySurface(el: Element | null): boolean {
  // `[contenteditable]:not([contenteditable="false"])` matches every editable form
  // — `contenteditable`, `="true"`, `="plaintext-only"` — while excluding the
  // explicitly non-editable `="false"` (Codex): otherwise ⌥W would close the window
  // while the user types in a `<div contenteditable>` Show Page editor.
  return !!el?.closest?.(
    'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], .monaco-editor, .xterm',
  );
}
