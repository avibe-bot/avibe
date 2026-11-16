import type { ReactNode } from 'react';
import { MessageCirclePlus } from 'lucide-react';

type RuntimeEntryMode = 'ios' | 'mac';
type RuntimeEntryState = 'expanded' | 'collapsed' | 'annotating';

const content = {
  ios: {
    title: 'Add to Home Screen',
    body: 'In Safari, tap Share, then Add to Home Screen.',
    cta: 'Add to Home Screen',
  },
  mac: {
    title: 'Add to Dock',
    body: 'In Safari, choose File, then Add to Dock.',
    cta: 'Add to Dock',
  },
} as const;

const RuntimeEntryButton = ({
  children,
  ariaLabel,
}: {
  children: ReactNode;
  ariaLabel: string;
}) => (
  <button
    type="button"
    aria-label={ariaLabel}
    className="rounded-full border border-border bg-surface px-3 py-2 text-xs font-semibold text-foreground shadow-elevation-1"
  >
    {children}
  </button>
);

export const ShowPageSystemEntryRuntimePreview = ({
  mode,
  state,
}: {
  mode: RuntimeEntryMode;
  state: RuntimeEntryState;
}) => {
  const copy = content[mode];

  return (
    <div className="relative h-full overflow-hidden bg-background text-foreground">
      <main className={`mx-auto flex h-full max-w-4xl flex-col px-6 pt-8 sm:px-10 ${state === 'expanded' ? 'pb-72' : 'pb-24'}`}>
        <div className="flex items-center justify-between border-b border-border pb-4">
          <div>
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-muted">Show Page</div>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight">Q4 Roadmap</h1>
          </div>
          <div className="rounded-full bg-wash px-3 py-1.5 text-xs text-muted">Public view</div>
        </div>
        <section className="mt-8 rounded-2xl border border-border bg-surface p-5 shadow-elevation-0">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-base font-semibold">Launch readiness</h2>
              <p className="mt-1 text-sm leading-relaxed text-muted">
                A public Show Page keeps the authored content in the foreground while the system entry stays compact.
              </p>
            </div>
            <div className="rounded-lg bg-cyan-soft px-3 py-2 text-sm font-semibold text-cyan-ink">82%</div>
          </div>
          <div className="mt-6 grid grid-cols-3 gap-2">
            <div className="h-20 rounded-lg bg-wash" />
            <div className="h-28 rounded-lg bg-wash" />
            <div className="h-24 rounded-lg bg-wash" />
          </div>
        </section>
        <div className="mt-auto pt-8 text-xs text-muted">Next milestone: November launch review.</div>
      </main>

      <div className={`absolute left-3 z-10 max-w-[calc(100%-1.5rem)] ${state === 'expanded' ? 'bottom-24' : 'bottom-3'}`}>
        {state === 'expanded' ? (
          <section
            role="dialog"
            aria-labelledby="runtime-entry-title"
            className="w-80 max-w-[calc(100vw-1.5rem)] rounded-2xl border border-border bg-surface p-3.5 shadow-elevation-2"
          >
            <div className="flex items-start gap-2.5">
              <div className="min-w-0 flex-1">
                <h2 id="runtime-entry-title" className="text-sm font-semibold">{copy.title}</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted">{copy.body}</p>
              </div>
              <RuntimeEntryButton ariaLabel="Close guidance">×</RuntimeEntryButton>
            </div>
            <div className="mt-3">
              <RuntimeEntryButton ariaLabel="Collapse guidance">Collapse guidance</RuntimeEntryButton>
            </div>
          </section>
        ) : null}
        {state === 'collapsed' ? (
          <RuntimeEntryButton ariaLabel={copy.title}>{copy.cta}</RuntimeEntryButton>
        ) : null}
      </div>
      {state === 'annotating' ? (
        <div className="absolute bottom-6 right-6 flex items-center gap-3 rounded-2xl border border-border bg-surface p-3 shadow-elevation-2">
          <MessageCirclePlus className="size-5 text-cyan-ink" />
          <span className="text-xs font-medium">Smart</span>
          <span className="text-xs font-medium">Screenshot</span>
          <span className="text-xs text-muted">Exit</span>
        </div>
      ) : (
        <button
          type="button"
          aria-label="Annotation mode"
          className="absolute bottom-6 right-6 grid size-13 place-items-center rounded-2xl border border-border bg-surface text-cyan-ink shadow-elevation-2"
        >
          <MessageCirclePlus className="size-5" />
        </button>
      )}
    </div>
  );
};
