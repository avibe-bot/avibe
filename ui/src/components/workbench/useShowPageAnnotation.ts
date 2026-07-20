import { useCallback, useEffect, useRef, useState } from 'react';

// postMessage bridge between the chat host and the annotation overlay running
// inside the chat's Show Page iframe (plan show-page-annotation-phase1 §3).
//
// This hook is owned by ChatPage, where the iframe lives. It sends control
// messages into the iframe and derives its `state` PURELY from the
// `avibe:annotation:state` messages the overlay broadcasts — the header control
// never optimistically flips itself, so it can't drift from the real overlay.

export type AnnotationMode = 'smart' | 'screenshot';

export interface AnnotationState {
  enabled: boolean;
  mode: AnnotationMode;
  /** False = overlay mounted but writes impossible (anonymous public visitor). */
  available: boolean;
}

export interface AnnotationBridge {
  /** Last state reported by the overlay; null until the first state message. */
  state: AnnotationState | null;
  /** Attach to the Show Page iframe so the bridge can target its window. */
  iframeRef: React.RefObject<HTMLIFrameElement | null>;
  /** Attach to the iframe `onLoad` to re-sync after a (re)load / re-point. */
  handleIframeLoad: () => void;
  /** `enable` without a mode uses the overlay's remembered mode (§3). */
  enable: (mode?: AnnotationMode) => void;
  disable: () => void;
  setMode: (mode: AnnotationMode) => void;
}

type ControlMessage =
  | { type: 'avibe:annotation:control'; action: 'enable' | 'disable'; mode?: AnnotationMode }
  | { type: 'avibe:annotation:control'; action: 'set-mode'; mode: AnnotationMode }
  | { type: 'avibe:annotation:query' };

/**
 * `src` is the current iframe URL; changing it (first open, or a private↔public
 * re-point, or a session switch that clears it) drops the derived state back to
 * "unknown" so the control disables until the freshly loaded overlay reports —
 * and so one session's state never briefly shows over another's page.
 */
export function useShowPageAnnotation(src: string | null): AnnotationBridge {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [state, setState] = useState<AnnotationState | null>(null);
  const [lastSrc, setLastSrc] = useState(src);

  // The loaded page changed — the new overlay hasn't reported yet, so drop back
  // to "unknown" until it does (and so one session's state never briefly shows
  // over another's page). Adjusting state during render is React's recommended
  // alternative to a reset-on-input effect (it re-renders before committing,
  // with no extra paint). https://react.dev/reference/react/useState
  if (src !== lastSrc) {
    setLastSrc(src);
    setState(null);
  }

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      // Same-origin iframe only: ignore other origins, and other windows (the
      // Show Page is same-origin and may talk to the parent for other reasons —
      // match by both source window and message type).
      if (event.origin !== window.location.origin) return;
      const frame = iframeRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const data = event.data as
        | { type?: unknown; enabled?: unknown; mode?: unknown; available?: unknown }
        | null;
      if (!data || data.type !== 'avibe:annotation:state') return;
      setState({
        enabled: data.enabled === true,
        mode: data.mode === 'screenshot' ? 'screenshot' : 'smart',
        available: data.available === true,
      });
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  const post = useCallback((message: ControlMessage) => {
    const win = iframeRef.current?.contentWindow;
    if (win) win.postMessage(message, window.location.origin);
  }, []);

  const enable = useCallback(
    (mode?: AnnotationMode) =>
      post(
        mode
          ? { type: 'avibe:annotation:control', action: 'enable', mode }
          : { type: 'avibe:annotation:control', action: 'enable' },
      ),
    [post],
  );
  const disable = useCallback(() => post({ type: 'avibe:annotation:control', action: 'disable' }), [post]);
  const setMode = useCallback(
    (mode: AnnotationMode) => post({ type: 'avibe:annotation:control', action: 'set-mode', mode }),
    [post],
  );
  // On (re)load the overlay broadcasts its state on mount, but the parent
  // listener is already attached, so we also query as a backstop (§3).
  const handleIframeLoad = useCallback(() => post({ type: 'avibe:annotation:query' }), [post]);

  return { state, iframeRef, handleIframeLoad, enable, disable, setMode };
}
