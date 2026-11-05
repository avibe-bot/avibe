import { useEffect, type ReactNode } from 'react';

import type { DesignLang, DesignTheme, Frame } from './frame';
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
      <DesignShell fixtures={frame.fixtures} route={frame.route} theme={theme}>
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
    // Ready once fonts are in and the providers' fixture reads have settled, including a read that
    // starts on a timer when the frame says to wait for it.
    // `designStage` names the step a frame that never gets ready is stuck in (main.tsx sets `render`).
    const stage = (name: string) => () => {
      document.body.dataset.designStage = name;
    };
    stage('fonts')();
    void document.fonts.ready
      .then(stage('network'))
      .then(() => designNetwork.quiet())
      .then(stage('wait'))
      .then(() => (frame.wait ? new Promise((resolve) => setTimeout(resolve, frame.wait)).then(() => designNetwork.quiet()) : undefined))
      .then(() => {
        if (cancelled) return;
        // A dialog moves focus into itself as it opens. A person who opened it with the
        // pointer sees no focus ring there, but a document nobody has touched draws one.
        if (document.activeElement instanceof HTMLElement && document.activeElement !== document.body) document.activeElement.blur();
        document.body.dataset.designReady = 'true';
      });
    return () => {
      cancelled = true;
    };
  }, [frame, theme]);
  // A frame that plays the product's own one-shot motion loops it: every `replay` ms, its `motion-*`
  // animations start over (the page's other animations, a skeleton's reveal, are left alone).
  useEffect(() => {
    if (!frame.replay) return;
    const timer = window.setInterval(() => {
      for (const animation of document.getAnimations()) {
        if (!(animation instanceof CSSAnimation) || !animation.animationName.startsWith('motion-')) continue;
        if (animation.effect?.getTiming().iterations === Infinity) continue;
        animation.currentTime = 0;
        animation.play();
      }
    }, frame.replay);
    return () => window.clearInterval(timer);
  }, [frame]);
  return <FrameBox frame={frame} theme={theme} />;
}

const STATUS_CLASS: Record<string, string> = {
  proposed: 'border-gold/40 text-gold-ink',
  approved: 'border-cyan/40 text-cyan-ink',
  shipped: 'border-mint/40 text-mint-ink',
};

/** Every frame, grouped by file, each theme side by side: the artboards. */
function Canvas({ lang, frames }: { lang: DesignLang; frames: Frame[] }) {
  const files = [...new Set(frames.map((f) => f.file))];
  return (
    <div data-theme="light" className="min-h-screen bg-muted-soft px-10 py-8 text-foreground">
      <h1 className="text-[22px] font-bold">Avibe Design</h1>
      <p className="mt-1 text-[13px] text-muted">
        {frames.length} frames · ?frame=&lt;id&gt;&amp;theme=light|dark&amp;lang=zh|en renders one frame at its size
      </p>
      {files.map((file) => (
        <section key={file} className="mt-10">
          <h2 className="font-mono text-[12px] font-bold uppercase tracking-[0.16em] text-muted">{file}</h2>
          <div className="mt-4 flex flex-wrap items-start gap-10">
            {frames.filter((f) => f.file === file).map((frame) => (
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

/** The canvas, or one frame when the URL names it (`?frame=<id>&theme=light|dark`). `frames` is every
 *  frame for the canvas; `frame` is the one the URL names, loaded alone. */
export function DesignRoot({ params, frames, frame }: { params: URLSearchParams; frames: Frame[]; frame?: Frame }): ReactNode {
  const lang: DesignLang = params.get('lang') === 'en' ? 'en' : 'zh';
  // `?list` renders nothing: the screenshot tool reads `window.__DESIGN__.frames` from it.
  if (params.has('list')) return null;
  const id = params.get('frame');
  if (!id) return <Canvas lang={lang} frames={frames} />;
  if (!frame) return <p>Unknown frame {id}</p>;
  return <Single frame={frame} theme={params.get('theme') === 'dark' ? 'dark' : 'light'} />;
}
