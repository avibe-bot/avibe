// A proposed visual direction around a shipped surface (directions.css): the surface renders
// unchanged, and the direction's tokens and component rules apply inside this root only.
import type { ReactNode } from 'react';

import './directions.css';

export type DirectionName = 'a' | 'b' | 'c';

export function Direction({ name, children }: { name: DirectionName; children: ReactNode }) {
  return (
    <div data-direction={name} className="relative h-full w-full overflow-hidden bg-background text-foreground">
      {children}
    </div>
  );
}
