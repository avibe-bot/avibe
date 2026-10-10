import { useLayoutEffect, type ReactNode } from 'react';

import './pet-window.css';

/**
 * Route shell for `/pet`. The transparent window style has to outlive
 * AuthGuard: on startup and on every authorization recheck the guard paints
 * its own loading state and unmounts the page, and the native window is
 * transparent, so an opaque body would show as a solid square.
 */
export function PetWindow({ children }: { children: ReactNode }) {
  useLayoutEffect(() => {
    document.documentElement.classList.add('pet-window');
    return () => document.documentElement.classList.remove('pet-window');
  }, []);
  return children;
}
