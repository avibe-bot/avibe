import { createContext, useContext } from 'react';

import type { ToastAction } from './toastCoalesce';

export type ToastType = 'success' | 'error' | 'warning';

interface ToastContextType {
  /** `persist`: the toast asks the person something and stays until they close it. */
  showToast: (message: string, type?: ToastType, action?: ToastAction, options?: { persist?: boolean }) => void;
}

export const ToastContext = createContext<ToastContextType | null>(null);

export const useToast = () => {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within a ToastProvider');
  }
  return context;
};
