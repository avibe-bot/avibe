// A sheet on a phone (docs/plans/web-ui-motion.md M6): it rises from the bottom edge on the sheet spring
// while the page behind dims, and leaves faster than it came. It can be dragged down: it follows the
// finger, springs back from a short pull, and a long or quick one dismisses it with the finger's speed.
import { NewSessionSheet } from '@/components/workbench/NewSessionSheet';

import { cut, spring, timed, type Curve, type Track } from '../../kit/motion';
import { MotionLoop } from '../../kit/MotionLoop';
import { TouchPoint } from '../../kit/TouchPoint';
import { inLayer, type Tier } from './fixtures';
import { InboxAt, Layer, PhoneStage } from './stage';

/** The sheet and its scrim, which the dialog portals to the document body. */
const SHEET = '[role="dialog"][data-state="open"]';
const SCRIM = 'div[data-state="open"].fixed.inset-0';

const OPEN_AT = 600;
const CLOSE_AT = 2400;
const PERIOD = 3600;

function sheetTracks(tier: Tier): Track[] {
  if (tier === 'now' || tier === 'reduced') {
    // Today: tw-animate's slide with a fade, 300 ms in and 200 ms out on `ease`, the scrim on 150 ms.
    const now = tier === 'now';
    const open: Curve = now ? { dur: 300, ease: 'ease' } : timed('fast', 'enter');
    const close: Curve = now ? { dur: 200, ease: 'ease' } : timed('fast', 'exit');
    const scrim: Curve = now ? { dur: 150, ease: 'ease' } : timed('fast', 'enter');
    return [
      {
        el: SHEET,
        global: true,
        from: { opacity: 0, y: now ? '100%' : 0 },
        beats: [
          { at: OPEN_AT, to: { opacity: 1, y: '0%' }, curve: open },
          { at: CLOSE_AT, to: { opacity: 0, y: now ? '100%' : '0%' }, curve: close },
        ],
      },
      { el: SCRIM, global: true, from: { opacity: 0 }, beats: [{ at: OPEN_AT, to: { opacity: 1 }, curve: scrim }, { at: CLOSE_AT, to: { opacity: 0 }, curve: scrim }] },
    ];
  }
  const rise = tier === 'restrained' ? timed('slow', 'move') : spring('sheet');
  const tracks: Track[] = [
    {
      el: SHEET,
      global: true,
      from: { y: '100%' },
      beats: [
        { at: OPEN_AT, to: { y: '0%' }, curve: rise },
        { at: CLOSE_AT, to: { y: '100%' }, curve: timed('base', 'exit') },
      ],
    },
    {
      el: SCRIM,
      global: true,
      from: { opacity: 0 },
      beats: [
        { at: OPEN_AT, to: { opacity: 1 }, curve: timed('base', 'enter') },
        { at: CLOSE_AT, to: { opacity: 0 }, curve: timed('base', 'exit') },
      ],
    },
  ];
  if (tier === 'pronounced') {
    // The page behind steps back a little, as a native card sheet does.
    tracks.push({
      el: inLayer('page'),
      origin: 'top center',
      beats: [
        { at: OPEN_AT, to: { scale: 0.94 }, curve: rise },
        { at: CLOSE_AT, to: { scale: 1 }, curve: timed('base', 'exit') },
      ],
    });
  }
  return tracks;
}

export function SheetLoop({ tier }: { tier: Tier }) {
  return (
    <MotionLoop id={`sheet-${tier}`} period={PERIOD} rest={OPEN_AT + 1200} tracks={sheetTracks(tier)}>
      <PhoneStage>
        <Layer name="page" opaque><InboxAt state="after" /></Layer>
        <NewSessionSheet open onClose={() => {}} onOpen={() => {}} />
      </PhoneStage>
    </MotionLoop>
  );
}

// ----- Drag to dismiss -----

/** Where the finger takes the sheet: the top of the shipped sheet as it lays out at 390 × 844. */
const SHEET_GRAB_Y = 573;
const GRAB = 640;
const PULL = 120;
const SPRING_BACK = 1100;
const FLICK = 1700;
const FLICK_PULL = 260;
const LET_GO = 1950;
const BACK_IN = 3000;
const DRAG_PERIOD = 4000;

/** Finger-tracked: the sheet moves exactly with the finger, so its curve is linear. */
const follow = (dur: number): Curve => ({ dur, ease: 'linear' });

function dragTracks(): Track[] {
  const flick = timed('base', 'enter');
  return [
    {
      el: SHEET,
      global: true,
      beats: [
        { at: GRAB, to: { y: PULL }, curve: follow(SPRING_BACK - GRAB) },
        { at: SPRING_BACK, to: { y: 0 }, curve: spring('snappy') },
        { at: FLICK, to: { y: FLICK_PULL }, curve: follow(LET_GO - FLICK) },
        // Let go moving fast: it keeps going, decelerating, until it is off the screen.
        { at: LET_GO, to: { y: '100%' }, curve: flick },
        { at: BACK_IN, to: { y: '0%' }, curve: spring('sheet') },
      ],
    },
    {
      el: SCRIM,
      global: true,
      beats: [
        { at: LET_GO, to: { opacity: 0 }, curve: flick },
        { at: BACK_IN, to: { opacity: 1 }, curve: timed('base', 'enter') },
      ],
    },
    {
      el: '[data-touch-point]',
      from: { opacity: 0 },
      beats: [
        { at: GRAB - 120, to: { opacity: 1 }, curve: timed('instant', 'enter') },
        { at: GRAB, to: { y: PULL }, curve: follow(SPRING_BACK - GRAB) },
        { at: SPRING_BACK, to: { opacity: 0 }, curve: timed('instant', 'exit') },
        { at: SPRING_BACK + 100, to: { y: 0 }, curve: cut },
        { at: FLICK - 120, to: { opacity: 1 }, curve: timed('instant', 'enter') },
        { at: FLICK, to: { y: FLICK_PULL }, curve: follow(LET_GO - FLICK) },
        { at: LET_GO, to: { opacity: 0 }, curve: timed('instant', 'exit') },
        { at: LET_GO + 100, to: { y: 0 }, curve: cut },
      ],
    },
  ];
}

export function SheetDragLoop() {
  return (
    <MotionLoop id="sheet-drag" period={DRAG_PERIOD} rest={SPRING_BACK - 20} tracks={dragTracks()}>
      <PhoneStage>
        <Layer name="page" opaque><InboxAt state="after" /></Layer>
        <NewSessionSheet open onClose={() => {}} onOpen={() => {}} />
        <TouchPoint top={SHEET_GRAB_Y} left={195} />
      </PhoneStage>
    </MotionLoop>
  );
}
