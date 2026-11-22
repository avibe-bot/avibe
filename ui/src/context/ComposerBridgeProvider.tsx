import { useMemo, useState, type ReactNode } from 'react';

import { ComposerBridgeContext, type ComposeTarget, type ComposerInsertTarget } from './ComposerBridgeContext';

export const ComposerBridgeProvider = ({ children }: { children: ReactNode }) => {
  const [target, setTarget] = useState<ComposerInsertTarget | null>(null);
  const [compose, setCompose] = useState<ComposeTarget | null>(null);
  // Stable value identity (the setters are stable setState fns) so the value only
  // changes when an active composer target does — not on unrelated re-renders.
  const value = useMemo(() => ({ target, setTarget, compose, setCompose }), [target, compose]);
  return <ComposerBridgeContext.Provider value={value}>{children}</ComposerBridgeContext.Provider>;
};
