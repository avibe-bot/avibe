// The wires between the cards and the gateway, drawn from where those boxes
// actually are.
//
// The story's circuit can hardcode its paths because it stretches one viewBox over a
// fixed composition. This diagram cannot: its columns are percentages, its band
// heights scale with `--ob-u`, and a provider card can be missing. So the paths are
// measured — every endpoint is the real horizontal centre of a real element, read
// after layout and read again when the box changes size.
//
// The authored shape is a drop, a curve and a drop. In the reference's 976x40 band
// the wire leaves at y=12, flattens onto y=28 and lands at 40; those three numbers
// are 0.3, 0.7 and 1 of the band, so writing them as fractions of the measured
// height reproduces the frame exactly where it was drawn and holds at every tier
// without a second set of numbers.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties, FC } from 'react';

/** Where the fan-in enters the gateway, as the reference's 24 scaled by the band. */
const ENTRY_SPREAD = 24 / 40;
const LEAVE = 0.3;
const FLATTEN = 0.7;

type Geometry = { width: number; height: number; paths: string[] };

/** One drop-curve-drop from `sx` at the top to `tx` at the bottom. A wire that does
 *  not have to move sideways is drawn straight rather than as a flat curve. */
function wirePath(sx: number, tx: number, height: number): string {
  const round = (value: number) => Math.round(value * 100) / 100;
  if (Math.abs(sx - tx) < 0.5) return `M ${round(sx)} 0 V ${round(height)}`;
  const leave = round(height * LEAVE);
  const flatten = round(height * FLATTEN);
  return `M ${round(sx)} 0 V ${leave} Q ${round(sx)} ${flatten} ${round(tx)} ${flatten} V ${round(height)}`;
}

const centreIn = (element: Element, originX: number): number => {
  const box = element.getBoundingClientRect();
  return box.left + box.width / 2 - originX;
};

/**
 * @param direction `inbound` fans several sources into one gateway; `outbound` fans
 *   one gateway out to several destinations. The reference spreads the inbound
 *   landings across the gateway's top and leaves every outbound wire from its
 *   centre, which is what makes the two bands read as convergence and distribution
 *   rather than as one symmetric shape drawn twice.
 * @param endpointSelector the elements at the far side of the gateway.
 * @param pulse replays the arrival once when it turns true.
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
  pulse: boolean;
}> = ({ direction, stage, endpointSelector, pulse }) => {
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
    const spread = box.height * ENTRY_SPREAD;
    const offset = (index: number) => (index - (endpoints.length - 1) / 2) * spread;
    const paths = endpoints.map((endpoint, index) => {
      const endpointX = centreIn(endpoint, box.left);
      return direction === 'inbound'
        ? wirePath(endpointX, gatewayX + offset(index), box.height)
        : wirePath(gatewayX, endpointX, box.height);
    });

    setGeometry((previous) =>
      previous
        && previous.width === box.width
        && previous.height === box.height
        && previous.paths.length === paths.length
        && previous.paths.every((path, index) => path === paths[index])
        ? previous
        : { width: box.width, height: box.height, paths });
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
        <svg viewBox={`0 0 ${geometry.width} ${geometry.height}`} width={geometry.width} height={geometry.height}>
          {geometry.paths.map((path, index) => (
            <path key={`wire-${index}`} className="setup-wire" d={path} />
          ))}
          {pulse && geometry.paths.map((path, index) => (
            // The halo and the core travel together; staggering them across the fan
            // is what makes three wires read as one arrival rather than a flash.
            <g key={`pulse-${index}`} style={{ '--setup-pulse-delay': `${index * 90}ms` } as CSSProperties}>
              <path className="setup-wire-pulse setup-wire-pulse-halo" d={path} />
              <path className="setup-wire-pulse" d={path} />
            </g>
          ))}
        </svg>
      )}
    </div>
  );
};
