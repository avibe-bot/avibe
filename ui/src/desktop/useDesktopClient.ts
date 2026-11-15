import { createElement, useCallback, useEffect, useState } from 'react';
import { useUnsavedChangesActionGuard } from '../context/useUnsavedChangesActionGuard';
import {
  DESKTOP_CLIENT_STATE_EVENT, DESKTOP_PRESENT_EVENT, hasDesktopClient, isClientProjection,
  isDesktopClientView, isNewerClientState, presentDesktopClient,
  type DesktopClientProjection, type DesktopClientView,
} from './bridge';

export function useDesktopClientProjection(): DesktopClientProjection | null {
  const [state, setState] = useState<DesktopClientProjection | null>(() => {
    const initial = hasDesktopClient() && window.__AVIBE_DESKTOP_CLIENT_STATE__;
    return isClientProjection(initial) ? initial : null;
  });
  useEffect(() => {
    if (!hasDesktopClient()) return;
    const update = (event: Event) => {
      const next: unknown = (event as CustomEvent).detail;
      if (isClientProjection(next)) setState((previous) => isNewerClientState(next, previous) ? next : previous);
    };
    window.addEventListener(DESKTOP_CLIENT_STATE_EVENT, update);
    return () => window.removeEventListener(DESKTOP_CLIENT_STATE_EVENT, update);
  }, []);
  return state;
}

export function usePresentDesktopClient() {
  const authorize = useUnsavedChangesActionGuard();
  return useCallback((view: DesktopClientView | 'updates', presentationToken?: string) => {
    if (!hasDesktopClient()) return false;
    const permission = authorize();
    if (!permission) return false;
    // The token is captured here, so an approval given later still answers this request.
    permission.runNavigation(() => {
      presentDesktopClient(view, presentationToken);
    });
    return true;
  }, [authorize]);
}

function CompatibleDesktopClientPresentation() {
  const present = usePresentDesktopClient();
  useEffect(() => {
    if (!hasDesktopClient()) return;
    const handle = (event: Event) => {
      const detail: unknown = (event as CustomEvent).detail;
      const view: unknown = (detail as { view?: unknown } | null)?.view;
      if (!isDesktopClientView(view)) return;
      const token: unknown = (detail as { presentationToken?: unknown } | null)?.presentationToken;
      // Consumed even when the user refuses, so native must not fall back past the guard.
      event.preventDefault();
      present(view, typeof token === 'string' ? token : undefined);
    };
    window.addEventListener(DESKTOP_PRESENT_EVENT, handle);
    return () => window.removeEventListener(DESKTOP_PRESENT_EVENT, handle);
  }, [present]);
  return null;
}

export function DesktopClientPresentation() {
  return hasDesktopClient() ? createElement(CompatibleDesktopClientPresentation) : null;
}
