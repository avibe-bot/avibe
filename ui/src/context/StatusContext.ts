import { createContext, useContext } from 'react';

export interface RuntimeStatus {
  state?: string;
  last_action?: string;
  [key: string]: any;
}

interface StatusContextType {
  status: RuntimeStatus;
  health: boolean;
  refreshStatus: () => Promise<RuntimeStatus | null>;
  control: (action: string, payload?: any) => Promise<any>;
}

export const StatusContext = createContext<StatusContextType | undefined>(undefined);

export const useStatus = () => {
  const context = useContext(StatusContext);
  if (!context) {
    throw new Error('useStatus must be used within a StatusProvider');
  }
  return context;
};
