import { useEffect, useState } from 'react';
import { CodeXml, FileText, ListChecks, Pause, Play, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useReducedMotion } from 'framer-motion';
import { BackendIcon } from '../visual';
import { Button } from '../ui/button';
import { Card } from '../ui/card';
import { ASSISTANT_ORDER, collaborationFrame } from './collaborationTimeline';

export function CollaborationStory({ paused, onPausedChange }: {
  paused: boolean;
  onPausedChange: (paused: boolean) => void;
}) {
  const { t } = useTranslation();
  const reducedMotion = useReducedMotion() === true;
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (paused || reducedMotion) return;
    let previous = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now();
      const delta = now - previous;
      previous = now;
      setElapsed((value) => value + delta);
    }, 50);
    return () => window.clearInterval(timer);
  }, [paused, reducedMotion]);
  const frame = collaborationFrame(elapsed, reducedMotion);
  const paths = ['M240 112 H320', 'M560 112 H640', 'M760 228 V260 H120 V228'];
  const icons = [FileText, CodeXml, ListChecks];

  return (
    <section className="onboarding-story" aria-label={t('onboarding.story.label')}>
      <p className="sr-only">{t('onboarding.story.description')}</p>
      <div className="onboarding-collaboration" data-reduced-motion={reducedMotion}>
        <svg className="onboarding-wires" viewBox="0 0 880 272" aria-hidden="true">
          {paths.map((path) => <path key={path} d={path} fill="none" stroke="var(--border-strong)" />)}
          {frame.handoff && !reducedMotion && (
            <path data-testid="handoff-pulse" d={paths[frame.handoff.path]} fill="none"
              stroke="var(--mint)" strokeWidth="2" pathLength="1" strokeDasharray="0.06 1"
              strokeDashoffset={-frame.handoff.progress * 0.94} />
          )}
        </svg>
        <div className="onboarding-collaboration-cards">
          {ASSISTANT_ORDER.map((backend, index) => {
            const progress = frame.progress[index];
            const state = progress >= 1 ? 'complete' : progress > 0 ? 'working' : 'waiting';
            const Icon = icons[index];
            const phase = index === 0 && frame.summary ? 'summary' : state;
            return (
              <Card key={backend} className="onboarding-collaboration-card" data-active={frame.active === index}
                data-state={state} aria-label={t(`onboarding.story.${backend}.name`)}>
                <div className="onboarding-story-status" aria-hidden="true">
                  <Icon size={13} />
                  <span key={phase} className={paused ? '' : 'onboarding-status-copy'}>{t(`onboarding.story.${backend}.${phase}`)}</span>
                  <svg viewBox="0 0 14 14" className="onboarding-status-glyph">
                    <circle cx="7" cy="7" r="2.5" fill="currentColor" opacity={state === 'waiting' ? 0.5 : 0} />
                    <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--mint)" strokeWidth="1.2"
                      pathLength="1" strokeDasharray="1" strokeDashoffset={1 - Math.min(1, progress * 1500 / 850)}
                      transform="rotate(-90 7 7)" />
                    <path d="m4.4 7 1.7 1.8 3.7-3.6" fill="none" stroke="var(--mint)" strokeWidth="1.3"
                      pathLength="1" strokeDasharray="1" strokeDashoffset={1 - Math.max(0, Math.min(1, (progress * 1500 - 850) / 300))} />
                  </svg>
                </div>
                <div className={`onboarding-skeleton onboarding-skeleton-${backend}`} aria-hidden="true">
                  {Array.from({ length: index === 2 ? 4 : 6 }, (_, line) => {
                    const revealed = progress >= (line + 1) / (index === 2 ? 4 : 6);
                    return (
                      <div key={line} className="onboarding-skeleton-row" style={{ opacity: revealed ? 1 : 0.2 }}>
                        {index === 1 && <span className="onboarding-code-number">{line + 1}</span>}
                        {index === 2 && <span className="onboarding-test-check">{revealed ? '✓' : ''}</span>}
                        <span className="onboarding-skeleton-line" style={{ width: `${[90, 75, 84, 62, 80, 68][line]}%` }} />
                      </div>
                    );
                  })}
                </div>
                <div className="onboarding-story-identity">
                  <BackendIcon backend={backend} size={28} variant="glyph" aria-hidden="true" />
                  <strong>{t(`onboarding.story.${backend}.name`)}</strong>
                  <span>{t(`onboarding.story.${backend}.role`)}</span>
                </div>
              </Card>
            );
          })}
        </div>
        <p className="onboarding-story-caption">{t('onboarding.story.caption')}</p>
      </div>
      {!reducedMotion && (
        <div className="onboarding-motion-controls">
          <Button variant="ghost" size="icon" className="size-7" onClick={() => onPausedChange(!paused)}
            aria-label={t(paused ? 'onboarding.story.play' : 'onboarding.story.pause')}>
            {paused ? <Play size={12} /> : <Pause size={12} />}
          </Button>
          <Button variant="ghost" size="icon" className="size-7" onClick={() => { setElapsed(0); onPausedChange(false); }}
            aria-label={t('onboarding.story.replay')}><RotateCcw size={12} /></Button>
        </div>
      )}
    </section>
  );
}
