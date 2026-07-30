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

/** Bind the browser-safe close chord to one mounted same-origin Show Page frame. */
export function bindShowPageFrameCloseShortcut(
  iframe: HTMLIFrameElement,
  onClose: () => void,
): () => void {
  const onKeyDown = (event: KeyboardEvent) => {
    if (
      event.code !== 'KeyW' ||
      !event.altKey ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey
    ) {
      return;
    }
    let active: Element | null = null;
    try {
      active = iframe.contentDocument?.activeElement ?? null;
    } catch {
      active = null;
    }
    if (inTextEntrySurface(active)) return;
    event.preventDefault();
    onClose();
  };

  const attach = () => {
    try {
      // The WindowProxy can survive navigation, so remove before every load-time
      // attach to keep exactly one capture listener on the active document.
      iframe.contentWindow?.removeEventListener('keydown', onKeyDown, true);
      iframe.contentWindow?.addEventListener('keydown', onKeyDown, true);
    } catch {
      // Cross-origin frames are not expected for the private Show Page surface.
    }
  };

  attach();
  iframe.addEventListener('load', attach);
  return () => {
    iframe.removeEventListener('load', attach);
    try {
      iframe.contentWindow?.removeEventListener('keydown', onKeyDown, true);
    } catch {
      // The frame may already be torn down.
    }
  };
}

/** Resolve focused window chrome, including controls rendered in a body portal. */
export function windowIdForKeyboardTarget(target: Element | null, layer: Element | null): string | null {
  if (!target || !layer) return null;

  const windowRoot = target.closest?.('[data-window-id]');
  if (windowRoot && layer.contains(windowRoot)) {
    return windowRoot.getAttribute('data-window-id');
  }

  const portalledRoot = target.closest?.('[data-window-owner-id]');
  const ownerId = portalledRoot?.getAttribute('data-window-owner-id');
  if (!ownerId) return null;

  // A data attribute outside this layer cannot nominate an arbitrary window.
  for (const candidate of layer.querySelectorAll('[data-window-id]')) {
    if (candidate.getAttribute('data-window-id') === ownerId) return ownerId;
  }
  return null;
}
