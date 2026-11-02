// Layout for the design-system frames: a page of titled sections, each a row of labelled specimens.
// Only the arrangement lives here; every specimen is a shipped token class or component.
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export function Sheet({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('flex min-h-full flex-col gap-8 bg-background p-8 text-foreground', className)}>{children}</div>;
}

export function Section({ title, note, children, className }: { title: string; note?: string; children: ReactNode; className?: string }) {
  return (
    <section className="flex flex-col gap-3">
      <header className="flex items-baseline gap-3">
        <h2 className="text-[13px] font-semibold text-foreground">{title}</h2>
        {note && <p className="text-[12px] text-muted">{note}</p>}
      </header>
      <div className={cn('flex flex-wrap items-start gap-4', className)}>{children}</div>
    </section>
  );
}

/** One specimen and its name: the token or class it is drawn with, then what it is for. */
export function Specimen({ name, use, children, className }: { name: string; use?: string; children: ReactNode; className?: string }) {
  return (
    <figure className={cn('flex flex-col gap-2', className)}>
      {children}
      <figcaption className="flex flex-col gap-0.5">
        <span className="font-mono text-[11px] text-foreground">{name}</span>
        {use && <span className="text-[11px] text-muted">{use}</span>}
      </figcaption>
    </figure>
  );
}

/** A colour tile with its label printed in the colour it is paired with. */
export function Chip({ className, children }: { className: string; children?: ReactNode }) {
  return <div className={cn('flex h-12 w-36 items-center rounded-lg px-3 text-[13px] font-semibold', className)}>{children}</div>;
}
