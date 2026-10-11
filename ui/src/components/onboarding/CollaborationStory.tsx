import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { ArrowRight, Check, CodeXml, ListChecks, ScanEye, Split, type LucideIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { getBackendUiMeta } from '@/lib/agentBackends';
import { BackendIcon } from '../visual';
import { Card } from '../ui/card';
import { ASSISTANT_ORDER, RETURN_WIRE, SETUP_LINEUP, WORK_LINES, collaborationFrame } from './collaborationTimeline';
import { useOnboardingMotion } from './motion';

/**
 * Handoff geometry in the 1040x350 wire box, which the stylesheet sizes at the card's
 * height times 350/300 — the reference's own proportion. Taking the box's height
 * from the card and putting the handoffs at y=150 of 350 is what keeps them on the
 * cards' shared midline at every tier rather than only at the authored one. The x
 * coordinates are the card tracks' own edges in 1040 space — 242-wide cards with 24
 * between them, which is `--ob-track-gap` in onboarding.css — so the stretched viewBox
 * keeps every port on a card edge at every width, for however many cards the lineup
 * holds. The return loop is not in this box: see `ReturnWire`.
 */
const BOX = 1040;
const GAP = 24;
const TRACK = (BOX - GAP * (SETUP_LINEUP.length - 1)) / SETUP_LINEUP.length;
const left = (index: number) => index * (TRACK + GAP);
const right = (index: number) => left(index) + TRACK;
const middle = (index: number) => left(index) + TRACK / 2;
const WIRES = SETUP_LINEUP.slice(1).map((_, index) => `M${right(index)} 150H${left(index + 1)}`);
/** The return loop's legs and corner radius, in the same 1040 space as the handoffs. */
const RETURN_LEGS = { from: middle(SETUP_LINEUP.length - 1), to: middle(0), radius: 16 };
/** Only the handoff ends are drawn: the return loop leaves its cards unmarked. */
const PORTS: [number, number][] = SETUP_LINEUP.slice(1).flatMap((_, index) => [[right(index), 150], [left(index + 1), 150]]);

type StoryWork = 'handoffs' | 'code' | 'review' | 'tests';
/** What each assistant is shown doing: the coordinator hands the work out, then it is
 *  built, reviewed and tested. A backend the story has no part for draws nothing. */
const STORY: Partial<Record<string, { icon: LucideIcon; work: StoryWork }>> = {
  vibey: { icon: Split, work: 'handoffs' },
  claude: { icon: CodeXml, work: 'code' },
  codex: { icon: ScanEye, work: 'review' },
  opencode: { icon: ListChecks, work: 'tests' },
};
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

// Every skeleton line as its share of the block it is measured in, so the cards draw
// the widths design_desktop.pen draws at any scale. A code line is a share of the bar
// column that starts after the line number, a test line a share of the content box
// (the checkbox and its gap are laid beside it, not out of it).
const CODE_INDENT = [0, 10, 20, 20, 10, 0];
const CODE_BARS: [number, number][] = [[24.3, 30.4], [18.1, 43.3], [29.2, 23], [21.7, 33.5], [31.6, 14.6], [14.4, 0]];
/** The review reads a diff: the second line removed, the two after it added. */
const REVIEW_DIFF: Partial<Record<number, 'removed' | 'added'>> = { 1: 'removed', 2: 'added', 3: 'added' };
const TEST_BARS = [73.2, 61.6, 82.3, 54.5];

function CodeRows({ written, diff }: { written: number; diff?: typeof REVIEW_DIFF }) {
  return (
    <>
      {CODE_BARS.map(([first, second], line) => (
        <div key={line} data-diff={diff?.[line]}
          className={`onboarding-code-row onboarding-write-line ${written > line ? 'onboarding-write-visible' : ''}`}>
          <span className="onboarding-code-number">{line + 1}</span>
          {/* The indent is geometry, not type, so it scales with the diagram. */}
          <div className="onboarding-code-bars" style={{ paddingLeft: `calc(${CODE_INDENT[line]} * var(--ob-u))` }}>
            <Bar width={first} tone={line % 2 ? 'onboarding-bar-violet' : 'onboarding-bar-cyan'} />
            <Bar width={second} />
          </div>
        </div>
      ))}
    </>
  );
}

function Skeleton({ work, active, done, written }: { work: StoryWork; active: boolean; done: boolean; written: number }) {
  const { t } = useTranslation();
  if (work === 'handoffs') {
    // One row per assistant the coordinator hands a part to, revealed as it plans.
    return (
      <div className="onboarding-skeleton onboarding-skeleton-handoffs">
        {ASSISTANT_ORDER.map((backend, index) => (
          <div key={backend} className={`onboarding-handoff-row onboarding-write-line ${written > index * 2 ? 'onboarding-write-visible' : ''}`}>
            <span className="onboarding-handoff-step">{t(`onboarding.story.${backend}.step`)}</span>
            <ArrowRight size={12} strokeWidth={1.8} className="onboarding-handoff-arrow" />
            <span className="onboarding-handoff-name">{getBackendUiMeta(backend).label}</span>
          </div>
        ))}
      </div>
    );
  }
  if (work === 'code' || work === 'review') {
    return (
      <div className={`onboarding-skeleton onboarding-skeleton-code ${work === 'review' ? 'onboarding-skeleton-review' : ''}`}>
        {/* The reviewer is handed finished code: it arrives whole with the work, and the
            sweep across it is the review. */}
        <CodeRows written={work === 'review' ? (active || done ? WORK_LINES : 0) : written}
          diff={work === 'review' ? REVIEW_DIFF : undefined} />
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

type Handoff = { wire: number; progress: number } | null;

/** A single dash of a 100-unit path crossing it once, fading in and out at its ends. */
function Pulse({ d, progress, glowId }: { d: string; progress: number; glowId: string }) {
  const style = {
    strokeDashoffset: 16 - 100 * progress,
    opacity: Math.max(0, Math.min(1, progress / 0.08, (1 - progress) / 0.06)),
  };
  return (
    <g data-testid="handoff-pulse">
      <path className="onboarding-pulse-halo" d={d} pathLength={100} style={{ ...style, filter: `url(#${glowId})` }} />
      <path className="onboarding-pulse-core" d={d} pathLength={100} style={style} />
    </g>
  );
}

function Glow({ id }: { id: string }) {
  return <defs><filter id={id} x="-100%" y="-500%" width="300%" height="1100%"><feGaussianBlur stdDeviation="3" /></filter></defs>;
}

/** The wires are stretched to the card grid, so every coordinate stays proportional. */
function Circuit({ handoff }: { handoff: Handoff }) {
  const glowId = useId();
  return (
    <svg className="onboarding-wires" viewBox={`0 0 ${BOX} 350`} fill="none" preserveAspectRatio="none" aria-hidden="true">
      <Glow id={glowId} />
      {WIRES.map((d, index) => (
        <g key={d}>
          <path className="onboarding-wire" d={d} />
          {handoff?.wire === index && <Pulse d={d} progress={handoff.progress} glowId={glowId} />}
        </g>
      ))}
      {PORTS.map(([x, y]) => <circle key={`${x}-${y}`} className="onboarding-port" cx={x} cy={y} r={3} />)}
    </svg>
  );
}

/**
 * The return loop and the caption cut into it. The stylesheet hangs this box from the
 * cards' floor down to the line the other setup screens set their summary on, which is
 * the stage's floor rather than the diagram's — so its height is whatever the window
 * leaves, and a stretched viewBox would bend the corners differently at every size.
 * The path is drawn in the box's own pixels instead: the legs keep their 1040-space
 * tracks and the corners stay round.
 */
function ReturnWire({ handoff, returning, caption }: { handoff: Handoff; returning: boolean; caption: string }) {
  const glowId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => {
      const { width, height } = node.getBoundingClientRect();
      setSize((current) => (current.width === width && current.height === height ? current : { width, height }));
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const { width, height } = size;
  const scale = width / BOX;
  const from = RETURN_LEGS.from * scale;
  const to = RETURN_LEGS.to * scale;
  const radius = Math.min(RETURN_LEGS.radius * scale, height);
  const d = `M${from} 0V${height - radius}Q${from} ${height} ${from - radius} ${height}`
    + `H${to + radius}Q${to} ${height} ${to} ${height - radius}V0`;
  return (
    <div ref={ref} className="onboarding-return">
      <svg className="onboarding-return-wire" fill="none" aria-hidden="true">
        <Glow id={glowId} />
        <path className="onboarding-wire" d={d} />
        {handoff?.wire === RETURN_WIRE && <Pulse d={d} progress={handoff.progress} glowId={glowId} />}
      </svg>
      <p className="onboarding-story-caption" data-returning={returning}>{caption}</p>
    </div>
  );
}

/** The design has no playback controls, so the loop starts with the screen and owns itself. */
export function CollaborationStory({ active = true }: { active?: boolean }) {
  const { t } = useTranslation();
  const { ref, reducedMotion, running } = useOnboardingMotion(active);
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
          {SETUP_LINEUP.map((backend, index) => {
            const done = frame.done[index];
            const active = frame.active === index;
            const state = done ? 'complete' : active ? 'working' : 'waiting';
            const summary = index === 0 && frame.summary;
            const handedOff = { count: ASSISTANT_ORDER.length };
            const caption = summary ? t('onboarding.story.summary')
              : t(`onboarding.story.${backend}.${done ? 'complete' : 'working'}`, handedOff);
            const part = STORY[backend];
            const Icon = part?.icon;
            return (
              <Card key={backend} className="onboarding-collaboration-card" data-active={active}
                data-state={state} data-backend={backend} aria-label={getBackendUiMeta(backend).label}>
                {/* The same identity header the connection step wears, in the same
                    place: logo, then the name — with the assistant's role under it
                    here, and the next step's enable switch beside it there. Four
                    cards leave no room for a name and a role on one line. */}
                <div className="onboarding-card-identity">
                  <span className="onboarding-card-logo"><BackendIcon backend={backend} size={28} variant="brand" aria-hidden="true" /></span>
                  <span className="onboarding-card-title">
                    <strong className="onboarding-card-name">{getBackendUiMeta(backend).label}</strong>
                    <span className="onboarding-card-role">{t(`onboarding.story.${backend}.role`)}</span>
                  </span>
                </div>
                <div className="onboarding-story-status" aria-hidden="true">
                  {Icon && <Icon size={13} strokeWidth={1.7} className="onboarding-status-icon" />}
                  <span key={caption} className="onboarding-status-text onboarding-status-enter">
                    {caption}
                  </span>
                  <WorkStatus active={active} done={done} progress={frame.progress[index]} />
                </div>
                {part && <Skeleton work={part.work} active={active} done={done}
                  written={reducedMotion ? WORK_LINES : frame.written[index]} />}
              </Card>
            );
          })}
        </div>
      </div>
      <ReturnWire handoff={frame.handoff} returning={frame.returning}
        caption={t(frame.returning ? 'onboarding.story.returnCaption' : 'onboarding.story.caption')} />
    </section>
  );
}
