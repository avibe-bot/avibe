import { useEffect, useId, useState } from 'react';
import { Check, CodeXml, FileText, ListChecks } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { BackendIcon } from '../visual';
import { Card } from '../ui/card';
import { ASSISTANT_ORDER, WORK_LINES, collaborationFrame } from './collaborationTimeline';
import { useOnboardingMotion } from './motion';

/** Wire geometry in the 880x272 collaboration box: two handoffs and the rounded return loop. */
const WIRES = [
  'M240 118H320',
  'M560 118H640',
  'M760 228V246Q760 262 744 262H136Q120 262 120 246V228',
];
const PORTS: [number, number][] = [[240, 118], [320, 118], [560, 118], [640, 118], [760, 228], [120, 228]];
const ICONS = [FileText, CodeXml, ListChecks];
// Every skeleton line as its share of the block it is measured in, so the three cards
// draw the widths design_desktop.pen draws at any scale. The card's content box is 198
// wide; a document line is a share of that, a test line too (the checkbox and its gap
// are laid beside it, not out of it), and a code line is a share of the bar column that
// starts after the line number — which is why those two sets have different bases.
const DOCUMENT_BARS = [55.1, 94.9, 79.3, 36.9, 89.4, 65.7];
const CODE_INDENT = [0, 10, 20, 20, 10, 0];
const CODE_BARS: [number, number][] = [[24.3, 30.4], [18.1, 43.3], [29.2, 23], [21.7, 33.5], [31.6, 14.6], [14.4, 0]];
const TEST_BARS = [73.2, 61.6, 82.3, 54.5];

function Bar({ width, tone, revealed }: { width: number; tone?: string; revealed?: boolean }) {
  const reveal = revealed === undefined ? ''
    : `onboarding-write-line ${revealed ? 'onboarding-write-visible' : ''}`;
  return <span className={`onboarding-bar ${tone ?? ''} ${reveal}`} style={{ width: `${width}%` }} />;
}

/**
 * One ring stays mounted from the first frame of work through completion: it draws itself as the
 * work progresses, closes, and the check then grows from its hinge toward both ends.
 */
function WorkStatus({ active, done, progress }: { active: boolean; done: boolean; progress: number }) {
  return (
    <span className="onboarding-status-glyph" aria-hidden="true">
      {active || done ? (
        <svg className="onboarding-status-mark" viewBox="0 0 20 20" fill="none">
          <circle className="onboarding-status-ring" cx="10" cy="10" r="8" pathLength="1"
            transform="rotate(-90 10 10)" style={{ strokeDashoffset: done ? 0 : 1 - progress }} />
          {done && (
            <g className="onboarding-status-check">
              <path d="M8.5 12.5 5.5 9.5" />
              <path d="M8.5 12.5 14.5 6.5" />
            </g>
          )}
        </svg>
      ) : <span className="onboarding-status-dot" />}
    </span>
  );
}

function Skeleton({ backend, done, written }: { backend: string; done: boolean; written: number }) {
  if (backend === 'claude') {
    return (
      <div className="onboarding-skeleton onboarding-skeleton-document">
        <Bar width={DOCUMENT_BARS[0]} tone="onboarding-bar-heading" revealed={written > 0} />
        <Bar width={DOCUMENT_BARS[1]} revealed={written > 1} />
        <Bar width={DOCUMENT_BARS[2]} revealed={written > 2} />
        <div className="onboarding-skeleton-paragraph">
          <Bar width={DOCUMENT_BARS[3]} tone="onboarding-bar-subheading" revealed={written > 3} />
          <Bar width={DOCUMENT_BARS[4]} revealed={written > 4} />
          <Bar width={DOCUMENT_BARS[5]} revealed={written > 5} />
        </div>
      </div>
    );
  }
  if (backend === 'codex') {
    return (
      <div className="onboarding-skeleton onboarding-skeleton-code">
        {CODE_BARS.map(([first, second], line) => (
          <div key={line} className={`onboarding-code-row onboarding-write-line ${written > line ? 'onboarding-write-visible' : ''}`}>
            <span className="onboarding-code-number">{line + 1}</span>
            {/* The indent is geometry, not type, so it scales with the diagram. */}
            <div className="onboarding-code-bars" style={{ paddingLeft: `calc(${CODE_INDENT[line]} * var(--ob-u))` }}>
              <Bar width={first} tone={line % 2 ? 'onboarding-bar-violet' : 'onboarding-bar-cyan'} />
              <Bar width={second} />
            </div>
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className={`onboarding-skeleton onboarding-skeleton-tests ${done ? 'onboarding-tests-done' : ''}`}>
      {TEST_BARS.map((width, line) => (
        <div key={line} className="onboarding-test-row" style={{ '--test-index': line } as React.CSSProperties}>
          <span className="onboarding-test-check"><Check size={10} strokeWidth={3} /></span>
          <Bar width={width} />
        </div>
      ))}
    </div>
  );
}

/** The wires are stretched to the card grid, so every coordinate stays proportional. */
function Circuit({ handoff }: { handoff: { wire: number; progress: number } | null }) {
  const glowId = useId();
  const pulse = handoff && {
    strokeDashoffset: 16 - 100 * handoff.progress,
    opacity: Math.max(0, Math.min(1, handoff.progress / 0.08, (1 - handoff.progress) / 0.06)),
  };
  return (
    <svg className="onboarding-wires" viewBox="0 0 880 272" fill="none" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <filter id={glowId} x="-100%" y="-500%" width="300%" height="1100%"><feGaussianBlur stdDeviation="3" /></filter>
      </defs>
      {WIRES.map((d, index) => (
        <g key={d}>
          <path className="onboarding-wire" d={d} />
          {handoff?.wire === index && (
            <g data-testid="handoff-pulse">
              <path className="onboarding-pulse-halo" d={d} pathLength={100} style={{ ...pulse, filter: `url(#${glowId})` }} />
              <path className="onboarding-pulse-core" d={d} pathLength={100} style={pulse ?? undefined} />
            </g>
          )}
        </g>
      ))}
      {PORTS.map(([x, y]) => <circle key={`${x}-${y}`} className="onboarding-port" cx={x} cy={y} r={3} />)}
    </svg>
  );
}

/** The design has no playback controls, so the loop starts with the screen and owns itself. */
export function CollaborationStory() {
  const { t } = useTranslation();
  const { ref, reducedMotion, running } = useOnboardingMotion();
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!running) return;
    let previous = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now();
      const delta = now - previous;
      previous = now;
      setElapsed((value) => value + delta);
    }, 16);
    return () => window.clearInterval(timer);
  }, [running]);
  const frame = collaborationFrame(elapsed, reducedMotion);

  return (
    <section className="onboarding-story" aria-label={t('onboarding.story.label')}>
      <p className="sr-only">{t('onboarding.story.description')}</p>
      {/* `data-motion` hands the same running state to the stylesheet: when the diagram
          is not being presented — the tab is hidden, or it has been scrolled out of
          sight — it holds every CSS animation inside at its current frame instead of
          letting it play on unseen against a stopped clock. The ref is what makes the
          second of those readable: it is this element's own visibility that decides. */}
      <div ref={ref} className="onboarding-collaboration" data-reduced-motion={reducedMotion}
        data-motion={running ? 'running' : 'paused'}>
        <Circuit handoff={frame.handoff} />
        <div className="onboarding-collaboration-cards">
          {ASSISTANT_ORDER.map((backend, index) => {
            const done = frame.done[index];
            const active = frame.active === index;
            const state = done ? 'complete' : active ? 'working' : 'waiting';
            const summary = index === 0 && frame.summary;
            const caption = summary ? t('onboarding.story.claude.summary')
              : t(`onboarding.story.${backend}.${done ? 'complete' : 'working'}`);
            const Icon = ICONS[index];
            return (
              <Card key={backend} className="onboarding-collaboration-card" data-active={active}
                data-state={state} aria-label={t(`onboarding.story.${backend}.name`)}>
                <div className="onboarding-story-status" aria-hidden="true">
                  <Icon size={13} strokeWidth={1.7} className="onboarding-status-icon" />
                  <span key={caption} className="onboarding-status-text onboarding-status-enter">
                    {caption}
                  </span>
                  <WorkStatus active={active} done={done} progress={frame.progress[index]} />
                </div>
                <Skeleton backend={backend} done={done} written={reducedMotion ? WORK_LINES : frame.written[index]} />
                <div className="onboarding-story-identity">
                  <BackendIcon backend={backend} size={28} variant="glyph" aria-hidden="true" />
                  <strong>{t(`onboarding.story.${backend}.name`)}</strong>
                  <span>{t(`onboarding.story.${backend}.role`)}</span>
                </div>
              </Card>
            );
          })}
        </div>
        <p className="onboarding-story-caption" data-returning={frame.returning}>
          {t(frame.returning ? 'onboarding.story.returnCaption' : 'onboarding.story.caption')}
        </p>
      </div>
    </section>
  );
}
