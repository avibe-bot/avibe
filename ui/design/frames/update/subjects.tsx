// The views the update/instance-and-app frames lay their panels and rows out in.
import type { ReactNode } from 'react';

import { UpdateSubjectsPanel, type SubjectLang, type UpdateSubjectSection } from '../../kit/update-subjects';

export type Subjects = { instance: UpdateSubjectSection; app: Omit<UpdateSubjectSection, 'name'> };
export type PanelState = { name: string; zh: string; en: string; subjects: Subjects };

export function Card({ children }: { children: ReactNode }) {
  return <div className="w-80 overflow-hidden rounded-xl border border-edge bg-popover shadow-elevation-2">{children}</div>;
}

export function Captioned({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <div className="flex w-80 flex-col gap-3">
      <div>
        <p className="text-sm font-semibold text-foreground">{title}</p>
        {note && <p className="text-xs text-muted">{note}</p>}
      </div>
      {children}
    </div>
  );
}

/** Every state of the two-section panel, side by side. */
export function States({ lang, states }: { lang: SubjectLang; states: PanelState[] }) {
  return (
    <div lang={lang} className="grid h-full grid-cols-4 content-start items-start justify-center gap-8 bg-background p-8 text-foreground">
      {states.map((state) => (
        <Captioned key={state.name} title={lang === 'en' ? state.en : state.zh}>
          <Card><UpdateSubjectsPanel lang={lang} {...state.subjects} /></Card>
        </Captioned>
      ))}
    </div>
  );
}

/** The sidebar's bottom row in each state, at the sidebar's width. */
export function Badges({ lang, rows }: { lang: SubjectLang; rows: [string, ReactNode][] }) {
  return (
    <div lang={lang} className="flex h-full flex-col gap-5 bg-background p-8 text-foreground">
      {rows.map(([caption, row]) => (
        <div key={caption} className="space-y-2">
          <p className="text-xs text-muted">{caption}</p>
          <div className="w-64 rounded-lg border border-edge bg-surface px-2 py-1">{row}</div>
        </div>
      ))}
    </div>
  );
}
