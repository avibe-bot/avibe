import { useCallback, useEffect, useRef, useState } from 'react';

import { actionShortcutMatches, useActionShortcuts } from '../../lib/actionShortcuts';
import { useLatestRef } from '../../lib/useLatestRef';
import { bindFrameChord } from '../apps/windowChords';
import { inShortcutBlockingOverlay } from './chatShortcuts';

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
  setIframe: React.RefCallback<HTMLIFrameElement>;
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

const PARENT_ESCAPE_CLAIM_SELECTOR =
  'input, textarea, [contenteditable]:not([contenteditable="false"])';

/**
 * `src` is the current iframe URL; changing it (first open, or a private↔public
 * re-point, or a session switch that clears it) drops the derived state back to
 * "unknown" so the control disables until the freshly loaded overlay reports —
 * and so one session's state never briefly shows over another's page.
 */
export function useShowPageAnnotation(src: string | null, shortcutActive = true): AnnotationBridge {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const frameShortcutCleanupRef = useRef<() => void>(() => undefined);
  const [state, setState] = useState<AnnotationState | null>(null);
  const [lastSrc, setLastSrc] = useState(src);
  const { showPageAnnotation: annotationShortcut } = useActionShortcuts();
  const stateRef = useLatestRef(state);
  const shortcutActiveRef = useLatestRef(shortcutActive);
  const shortcutRef = useLatestRef(annotationShortcut);

  // The loaded page changed — the new overlay hasn't reported yet, so drop back
  // to "unknown" until it does (and so one session's state never briefly shows
  // over another's page). Adjusting state during render is React's recommended
  // alternative to a reset-on-input effect (it re-renders before committing,
  // with no extra paint). https://react.dev/reference/react/useState
  if (src !== lastSrc) {
    setLastSrc(src);
    setState(null);
  }

  const onMessage = useCallback((event: MessageEvent) => {
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
  }, []);

  const listeningRef = useRef(false);
  const startListening = useCallback(() => {
    if (listeningRef.current) return;
    window.addEventListener('message', onMessage);
    listeningRef.current = true;
  }, [onMessage]);
  const stopListening = useCallback(() => {
    if (!listeningRef.current) return;
    window.removeEventListener('message', onMessage);
    listeningRef.current = false;
  }, [onMessage]);

  // Keep the listener alive even while `src` is null. A restored/cached iframe
  // can report during its commit, before passive effects run; the callback ref
  // below attaches synchronously before that frame can finish loading.
  useEffect(() => {
    startListening();
    return stopListening;
  }, [startListening, stopListening]);

  const onParentKeyDown = useCallback((event: KeyboardEvent) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    const target = event.target;
    if (target instanceof Element && target.closest(PARENT_ESCAPE_CLAIM_SELECTOR)) return;
    if (inShortcutBlockingOverlay(target as Element | null, document)) return;

    try {
      const frameDocument = iframeRef.current?.contentDocument;
      if (!frameDocument) return;
      const FrameKeyboardEvent = frameDocument.defaultView?.KeyboardEvent ?? KeyboardEvent;
      frameDocument.dispatchEvent(
        new FrameKeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      );
    } catch {
      // The frame may be sandboxed, navigating, or already torn down.
    }
  }, []);

  const escapeListeningRef = useRef(false);
  const startEscapeListening = useCallback(() => {
    if (escapeListeningRef.current) return;
    window.addEventListener('keydown', onParentKeyDown);
    escapeListeningRef.current = true;
  }, [onParentKeyDown]);
  const stopEscapeListening = useCallback(() => {
    if (!escapeListeningRef.current) return;
    window.removeEventListener('keydown', onParentKeyDown);
    escapeListeningRef.current = false;
  }, [onParentKeyDown]);

  useEffect(() => {
    if (state?.enabled === true && iframeRef.current) startEscapeListening();
    else stopEscapeListening();
    return stopEscapeListening;
  }, [startEscapeListening, state?.enabled, stopEscapeListening]);

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
  const enableFromShortcut = useCallback(() => {
    const current = stateRef.current;
    if (current?.available !== true || current.enabled) return;
    post({ type: 'avibe:annotation:control', action: 'enable' });
  }, [post, stateRef]);

  const setIframe = useCallback<React.RefCallback<HTMLIFrameElement>>(
    (iframe) => {
      if (iframeRef.current !== iframe) {
        stopEscapeListening();
      }
      frameShortcutCleanupRef.current();
      frameShortcutCleanupRef.current = () => undefined;
      iframeRef.current = iframe;
      if (!iframe) return;
      startListening();
      if (stateRef.current?.enabled === true) startEscapeListening();
      frameShortcutCleanupRef.current = bindFrameChord(
        iframe,
        (event, activeInFrame) => {
          let frameDocument: Document | undefined;
          try {
            frameDocument = iframe.contentDocument ?? undefined;
          } catch {
            frameDocument = undefined;
          }
          return (
            !event.defaultPrevented
            && !event.repeat
            && shortcutActiveRef.current
            && stateRef.current?.available === true
            && stateRef.current.enabled !== true
            && actionShortcutMatches(event, shortcutRef.current)
            && !inShortcutBlockingOverlay(activeInFrame, frameDocument)
          );
        },
        enableFromShortcut,
      );
    },
    [
      enableFromShortcut,
      shortcutActiveRef,
      shortcutRef,
      startEscapeListening,
      startListening,
      stateRef,
      stopEscapeListening,
    ],
  );

  useEffect(() => () => frameShortcutCleanupRef.current(), []);

  useEffect(() => {
    if (!shortcutActive || state?.available !== true || state.enabled || !iframeRef.current) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented
        || event.repeat
        || !actionShortcutMatches(event, annotationShortcut)
      ) {
        return;
      }
      const target = event.target;
      if (inShortcutBlockingOverlay(target as Element | null, document)) return;
      event.preventDefault();
      enableFromShortcut();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [annotationShortcut, enableFromShortcut, shortcutActive, state?.available, state?.enabled]);

  // On (re)load the overlay broadcasts its state on mount, but the parent
  // listener is already attached, so we also query as a backstop (§3).
  const handleIframeLoad = useCallback(() => post({ type: 'avibe:annotation:query' }), [post]);

  return { state, setIframe, handleIframeLoad, enable, disable, setMode };
}
