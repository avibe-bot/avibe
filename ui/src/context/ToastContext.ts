import { createContext, useContext } from 'react';

import type { ToastAction } from './toastCoalesce';

export type ToastType = 'success' | 'error' | 'warning';

interface ToastContextType {
  showToast: (message: string, type?: ToastType, action?: ToastAction) => void;
}

export const ToastContext = createContext<ToastContextType | null>(null);

export const useToast = () => {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within a ToastProvider');
  }
  return context;
};
