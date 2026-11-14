// Design frames draw the protected seed-phrase wallet dialog in each of its states without the vault
// sandbox, which no frame may load: a frame provides the state here, and the dialog shows it as if
// the sandbox were pinned with the operation and the vault existed (ui/design: vault/mnemonic/protected/*).
import { createContext } from 'react';

import type { ProtectedHdPhase } from './useProtectedHdCreate';

/** The state to draw, and the wallet's name as if typed (a frame cannot type). */
export const ProtectedHdDesignState = createContext<{ phase: ProtectedHdPhase; name: string } | null>(null);
