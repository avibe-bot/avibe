import { useEffect, type ReactNode } from 'react';

import type { DesignLang, DesignTheme, Frame } from './frame';
import { ALL_FRAMES } from './registry';
import { DesignShell } from './shell/DesignShell';
import { designNetwork } from './shell/network';

function FrameBox({ frame, theme }: { frame: Frame; theme: DesignTheme }) {
  return (
    <div
      data-theme={theme}
      data-frame-root={frame.id}
      className="relative overflow-hidden bg-background text-foreground"
      style={{ width: frame.width, height: frame.height }}
    >
      <DesignShell fixtures={frame.fixtures} route={frame.route}>
        {frame.render()}
      </DesignShell>
    </div>
  );
}

/** One frame at its exact size: what the screenshot tool captures. */
function Single({ frame, theme }: { frame: Frame; theme: DesignTheme }) {
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    let cancelled = false;
    // Ready once fonts are in and the providers' fixture reads have settled.
    void document.fonts.ready
      .then(() => designNetwork.quiet())
      .then(() => {
        if (!cancelled) document.body.dataset.designReady = 'true';
      });
    return () => {
      cancelled = true;
    };
  }, [theme]);
  return <FrameBox frame={frame} theme={theme} />;
}

const STATUS_CLASS: Record<string, string> = {
  proposed: 'border-gold/40 text-gold-ink',
  approved: 'border-cyan/40 text-cyan-ink',
  shipped: 'border-mint/40 text-mint-ink',
};

/** Every frame, grouped by file, each theme side by side: the artboards. */
function Canvas({ lang }: { lang: DesignLang }) {
  const files = [...new Set(ALL_FRAMES.map((f) => f.file))];
  return (
    <div data-theme="light" className="min-h-screen bg-muted-soft px-10 py-8 text-foreground">
      <h1 className="text-[22px] font-bold">Avibe Design</h1>
      <p className="mt-1 text-[13px] text-muted">
        {ALL_FRAMES.length} frames · ?frame=&lt;id&gt;&amp;theme=light|dark&amp;lang=zh|en renders one frame at its size
      </p>
      {files.map((file) => (
        <section key={file} className="mt-10">
          <h2 className="font-mono text-[12px] font-bold uppercase tracking-[0.16em] text-muted">{file}</h2>
          <div className="mt-4 flex flex-wrap items-start gap-10">
            {ALL_FRAMES.filter((f) => f.file === file).map((frame) => (
              <figure key={frame.id} className="flex flex-col gap-2">
                <figcaption className="flex items-center gap-2 text-[12px]">
                  <span className="font-semibold">{frame.title}</span>
                  <span className={`rounded border px-1.5 py-px font-mono text-[10px] ${STATUS_CLASS[frame.status]}`}>{frame.status}</span>
                  <a className="font-mono text-[10px] text-muted hover:text-foreground" href={`?frame=${encodeURIComponent(frame.id)}&theme=light&lang=${lang}`}>
                    {frame.id}
                  </a>
                </figcaption>
                <div className="flex items-start gap-4">
                  {(frame.themes ?? ['light', 'dark']).map((theme) => (
                    // Each frame is its own document, so its theme, portals (popovers,
                    // dialogs), and router never leak into another frame.
                    <iframe
                      key={theme}
                      title={`${frame.id} ${theme}`}
                      src={`?frame=${encodeURIComponent(frame.id)}&theme=${theme}&lang=${lang}`}
                      className="rounded-xl border border-border bg-background shadow-sm"
                      style={{ width: frame.width, height: frame.height }}
                    />
                  ))}
                </div>
              </figure>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

/** The canvas, or one frame when the URL names it (`?frame=<id>&theme=light|dark`). */
export function DesignRoot({ params }: { params: URLSearchParams }): ReactNode {
  const lang: DesignLang = params.get('lang') === 'en' ? 'en' : 'zh';
  const id = params.get('frame');
  if (!id) return <Canvas lang={lang} />;
  const frame = ALL_FRAMES.find((f) => f.id === id);
  if (!frame) return <p>Unknown frame {id}</p>;
  return <Single frame={frame} theme={params.get('theme') === 'dark' ? 'dark' : 'light'} />;
}
