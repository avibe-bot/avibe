import { useMemo, useState, type ReactNode } from 'react';

import { ComposerBridgeContext, type ComposerInsertTarget } from './ComposerBridgeContext';

export const ComposerBridgeProvider = ({ children }: { children: ReactNode }) => {
  const [target, setTarget] = useState<ComposerInsertTarget | null>(null);
  // Stable value identity (setTarget is a stable setState fn) so the value only
  // changes when the active composer target does — not on unrelated re-renders.
  const value = useMemo(() => ({ target, setTarget }), [target]);
  return <ComposerBridgeContext.Provider value={value}>{children}</ComposerBridgeContext.Provider>;
};
