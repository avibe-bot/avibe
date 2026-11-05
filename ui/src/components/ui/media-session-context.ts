import * as React from 'react';

// The conversation an attachment card is shown in, so the card can keep its
// metadata with that conversation in the local message store and show it
// offline (docs/plans/local-sync-store.md section 4.6). The chat page provides
// it; elsewhere it is null and a card stores nothing.

export const MediaSessionContext = React.createContext<string | null>(null);

export function useMediaSession(): string | null {
  return React.useContext(MediaSessionContext);
}
