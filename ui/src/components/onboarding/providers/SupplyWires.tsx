// The wires between the cards and the gateway, drawn from where those boxes
// actually are.
//
// The story's circuit can hardcode its paths because it stretches one viewBox over a
// fixed composition. This diagram cannot: its columns are percentages, its band
// heights scale with `--ob-u`, and a provider card can be missing. So the paths are
// measured — every endpoint is the real horizontal centre of a real element, read
// after layout and read again when the box changes size.
//
// The authored shape is the reference's own: a short drop off the card, a rounded
// corner onto the band's midline, the run that gathers toward the gateway, a second
// corner, and the drop in. Those breakpoints are y=7, 19 and 31 of a 38-unit band, so
// writing them as fractions of the measured height reproduces the frame exactly where
// it was drawn and holds at every tier without a second set of numbers. Everything the
// wire is *made of* — stroke, port, pulse — is the story's, by class: one screen's
// circuit and this one are the same object seen twice, and there is one place to
// change what that object looks like.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { FC } from 'react';

/** The reference's y breakpoints as shares of its 38-unit band. */
const LEAVE = 7 / 38;
const RUN = 19 / 38;
const ENTER = 31 / 38;
/** The reference's corner, in px: the band scales vertically, the corner does not. */
const CORNER = 12;
/** The story draws its ports at r=3 in a 350-unit box; this band is the reference's
 *  own 38, and 2.5 is the dot it draws there. */
const PORT_R = 2.5;

type Geometry = { width: number; height: number; paths: string[]; ports: number[] };

const round = (value: number) => Math.round(value * 100) / 100;

/** One drop-corner-run-corner-drop from `sx` at the top to `tx` at the bottom. A wire
 *  that does not have to move sideways is drawn straight rather than as a flat curve. */
function wirePath(sx: number, tx: number, height: number): string {
  if (Math.abs(sx - tx) < 0.5) return `M ${round(sx)} 0 V ${round(height)}`;
  const leave = round(height * LEAVE);
  const run = round(height * RUN);
  const enter = round(height * ENTER);
  // A corner cannot be wider than half the run it turns, or the two would cross.
  const corner = Math.min(CORNER, Math.abs(tx - sx) / 2) * Math.sign(tx - sx);
  return `M ${round(sx)} 0 V ${leave} Q ${round(sx)} ${run} ${round(sx + corner)} ${run}`
    + ` H ${round(tx - corner)} Q ${round(tx)} ${run} ${round(tx)} ${enter} V ${round(height)}`;
}

const centreIn = (element: Element, originX: number): number => {
  const box = element.getBoundingClientRect();
  return box.left + box.width / 2 - originX;
};

/**
 * @param direction `inbound` gathers several sources into one gateway; `outbound` fans
 *   one gateway out to several destinations. Both land on the gateway's centre — the
 *   reference converges rather than spreading, which is what lets the two bands read
 *   as one route through the middle card instead of two symmetric fans.
 * @param endpointSelector the elements at the far side of the gateway.
 * @param stage the element every measurement is taken inside. Passed as the element
 *   rather than as a ref on purpose: a ref object's identity never changes, so the
 *   first measurement would have to happen before the parent's own ref was attached
 *   and nothing would ever ask for a second one. An element is a value — it arrives,
 *   the effects re-run, and the wires appear on the frame the stage exists.
 */
export const SupplyWires: FC<{
  direction: 'inbound' | 'outbound';
  stage: HTMLElement | null;
  endpointSelector: string;
}> = ({ direction, stage, endpointSelector }) => {
  const bandRef = useRef<HTMLDivElement | null>(null);
  const [geometry, setGeometry] = useState<Geometry | null>(null);

  const measure = useCallback(() => {
    const band = bandRef.current;
    if (!band || !stage) return;
    const box = band.getBoundingClientRect();
    const gateway = stage.querySelector('.setup-gateway');
    const endpoints = [...stage.querySelectorAll(endpointSelector)];
    // A hidden screen has no boxes to measure. Keeping the previous geometry rather
    // than drawing a degenerate one means returning to the screen shows wires, not
    // a flat line that corrects itself a frame later.
    if (!gateway || endpoints.length === 0 || box.width === 0 || box.height === 0) return;

    const gatewayX = centreIn(gateway, box.left);
    const ports = endpoints.map((endpoint) => round(centreIn(endpoint, box.left)));
    const paths = ports.map((endpointX) => (direction === 'inbound'
      ? wirePath(endpointX, gatewayX, box.height)
      : wirePath(gatewayX, endpointX, box.height)));

    setGeometry((previous) =>
      previous
        && previous.width === box.width
        && previous.height === box.height
        && previous.paths.length === paths.length
        && previous.paths.every((path, index) => path === paths[index])
        ? previous
        : { width: box.width, height: box.height, paths, ports });
  }, [direction, endpointSelector, stage]);

  useLayoutEffect(measure, [measure]);

  useEffect(() => {
    if (!stage || typeof ResizeObserver === 'undefined') return;
    // Watching the stage rather than the window catches the cases a resize event
    // misses: a card appearing after a scan, and a tier variable changing the band's
    // own height without the window changing at all.
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [measure, stage]);

  return (
    <div ref={bandRef} className={`setup-wires setup-wires--${direction}`} aria-hidden="true">
      {geometry && (
        <svg viewBox={`0 0 ${geometry.width} ${geometry.height}`} width={geometry.width} height={geometry.height} fill="none">
          {geometry.paths.map((path, index) => (
            <g key={`wire-${index}`}>
              <path className="onboarding-wire" d={path} fill="none" />
              {/* `pathLength` is what makes the pulse a pulse: the dash is 16 of 100,
                  so it is one segment crossing one wire whatever that wire measures.
                  Without it the 16 is 16 user units and a long wire wears the pattern
                  several times over — a row of chunks rather than a thing in motion.
                  The halo and the core travel together and undelayed: the whole fan
                  arriving at once is what reads as convergence. */}
              {/* The halo is a wider, softer stroke of the same path — the reference's
                  own glow. No blur filter: one on a band this short reads as an
                  over-exposed bar rather than as light around a moving segment. */}
              <path className="onboarding-pulse-halo" d={path} fill="none" pathLength={100} />
              <path className="onboarding-pulse-core" d={path} fill="none" pathLength={100} />
            </g>
          ))}
          {/* The far end of every wire, on the edge it leaves from. */}
          {geometry.ports.map((x, index) => (
            <circle key={`port-${index}`} className="onboarding-port" cx={x}
              cy={direction === 'inbound' ? 0 : geometry.height} r={PORT_R} />
          ))}
        </svg>
      )}
    </div>
  );
};
