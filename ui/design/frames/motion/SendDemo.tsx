// Sending a message (docs/plans/web-ui-motion.md M1, M2): the message rises out of the composer and
// settles in place while the conversation glides up to make room, Send turns into Stop, and the agent's
// working bubble fades in under it. If the send fails, the message is withdrawn the way it came and the
// text is back in the composer. Each tier is the same conversation at the same size; `now` is what the
// product does today (the turn starts at once, the message lands when the POST answers).
import { beat, cut, offset, scaleOf, spring, timed, type Curve, type Track } from '../../kit/motion';
import { MotionLoop } from '../../kit/MotionLoop';
import {
  COMPOSER,
  HEADER,
  inLayer,
  row,
  SEND,
  sent,
  SHIFT,
  STOP,
  TYPED,
  TRANSCRIPT,
  TRANSCRIPT_ROWS,
  WORKING,
  type Tier,
} from './fixtures';
import { ChatAt, DesktopStage, Layer, PhoneStage } from './stage';

/** When Send is pressed, and how long one loop runs. */
const SEND_AT = 700;
const PERIOD = 4400;
const RESET = 500;
/** The page pins its transcript to the bottom when its data lands; the loop starts after that. */
const PIN_FIRST = 1500;
/** Where the frame rests when animations are off: sent, and settled. */
const SETTLED = SEND_AT + 1500;
/** Today the message lands with the POST answer, this long after the working bubble. */
const POST_ANSWER = 450;

type Feel = {
  /** The conversation gliding up. */
  glide: Curve;
  /** The message: where it starts below its place, the scale it grows from, and its fade. */
  rise: number;
  grow: number;
  fade: Curve;
  /** The working bubble, a beat after the message. */
  dotsAfter: number;
  dotsRise: number;
  /** Stop growing in where Send was. */
  morph: number;
  press: number;
};

function feel(tier: Exclude<Tier, 'now' | 'reduced'>): Feel {
  // Press and the Send/Stop trade are controls: the same in every tier that has them.
  const press = scaleOf('press-control');
  const morph = scaleOf('morph');
  if (tier === 'restrained') {
    return { glide: timed('base', 'enter'), rise: 0, grow: 1, fade: timed('fast', 'enter'), dotsAfter: beat(), dotsRise: 0, morph: 1, press: 1 };
  }
  if (tier === 'pronounced') {
    return {
      glide: spring('bouncy'),
      rise: offset('xl'),
      grow: scaleOf('enter-lg'),
      fade: timed('base', 'enter'),
      dotsAfter: beat(),
      dotsRise: offset('md'),
      morph,
      press,
    };
  }
  return {
    glide: spring('smooth'),
    rise: offset('md'),
    grow: scaleOf('enter'),
    fade: timed('fast', 'enter'),
    dotsAfter: beat(),
    dotsRise: offset('sm'),
    morph,
    press,
  };
}

/** The pressed Send button, the before page handing over to the after page, and the after page
 *  starting where the before page was. */
function sendTracks(f: Feel, shift: number): Track[] {
  const before = (selector: string) => inLayer('before', selector);
  const after = (selector: string) => inLayer('after', selector);
  // The composer hands over in two beats, so the two texts never overlap: the typed text and Send
  // leave at once, then the empty composer and Stop come in.
  const leave = timed('instant', 'exit');
  const arrive = timed('fast', 'enter');
  const handover = SEND_AT + leave.dur;
  return [
    {
      el: before(SEND),
      beats: [
        { at: SEND_AT - 110, to: { scale: f.press }, curve: timed('instant', 'enter') },
        { at: SEND_AT, to: { opacity: 0, scale: f.morph }, curve: leave },
      ],
    },
    { el: before(`${COMPOSER} ${TYPED}`), beats: [{ at: SEND_AT, to: { opacity: 0 }, curve: leave }] },
    { el: before(TRANSCRIPT), beats: [{ at: SEND_AT, to: { opacity: 0 }, curve: cut }] },
    { el: before(HEADER), beats: [{ at: SEND_AT, to: { opacity: 0 }, curve: cut }] },
    // Gone once the new composer is opaque over it, so their shadows do not add up.
    { el: before(COMPOSER), beats: [{ at: handover + arrive.dur, to: { opacity: 0 }, curve: cut }] },
    { el: inLayer('after'), from: { opacity: 0 }, beats: [{ at: SEND_AT, to: { opacity: 1 }, curve: cut }] },
    // FLIP inside the scroller: the page has already pinned the new bottom, so the rows take the inverse
    // offset only now (an offset present when the page pinned would have moved the pin) and glide off it.
    {
      el: after(TRANSCRIPT_ROWS),
      beats: [
        { at: SEND_AT - 1, to: { y: shift }, curve: cut },
        { at: SEND_AT, to: { y: 0 }, curve: f.glide },
      ],
    },
    {
      el: after(row(sent.id)),
      origin: 'bottom right',
      from: { opacity: 0, y: f.rise, scale: f.grow },
      beats: [{ at: SEND_AT, to: { opacity: 1, y: 0, scale: 1 }, curve: f.glide, fade: f.fade }],
    },
    {
      el: after(WORKING),
      origin: 'bottom left',
      from: { opacity: 0, y: f.dotsRise },
      beats: [{ at: SEND_AT + f.dotsAfter, to: { opacity: 1, y: 0 }, curve: f.glide, fade: f.fade }],
    },
    { el: after(COMPOSER), from: { opacity: 0 }, beats: [{ at: handover, to: { opacity: 1 }, curve: arrive }] },
    { el: after(STOP), from: { scale: f.morph }, beats: [{ at: handover, to: { scale: 1 }, curve: spring('snappy') }] },
  ];
}

export function SendLoop({ tier, shift, desktop = false }: { tier: Tier; shift: number; desktop?: boolean }) {
  const Stage = desktop ? DesktopStage : PhoneStage;
  const id = `send-${desktop ? 'desktop-' : ''}${tier}`;
  if (tier === 'now') {
    // Opaque pages over each other, each removed in one frame.
    return (
      <MotionLoop
        delay={PIN_FIRST}
        id={id}
        period={PERIOD}
        reset={RESET}
        rest={SETTLED}
        tracks={[
          { el: inLayer('before'), beats: [{ at: SEND_AT, to: { opacity: 0 }, curve: cut }] },
          { el: inLayer('waiting'), beats: [{ at: SEND_AT + POST_ANSWER, to: { opacity: 0 }, curve: cut }] },
        ]}
      >
        <Stage>
          <Layer name="after" opaque><ChatAt state="after" /></Layer>
          <Layer name="waiting" opaque><ChatAt state="waiting" /></Layer>
          <Layer name="before" opaque><ChatAt state="before" /></Layer>
        </Stage>
      </MotionLoop>
    );
  }
  if (tier === 'reduced') {
    // No travel: the before page fades out over the after page.
    return (
      <MotionLoop
        delay={PIN_FIRST}
        id={id}
        period={PERIOD}
        reset={RESET}
        rest={SETTLED}
        tracks={[{ el: inLayer('before'), beats: [{ at: SEND_AT, to: { opacity: 0 }, curve: timed('fast', 'enter') }] }]}
      >
        <Stage>
          <Layer name="after" opaque><ChatAt state="after" /></Layer>
          <Layer name="before" opaque><ChatAt state="before" /></Layer>
        </Stage>
      </MotionLoop>
    );
  }
  return (
    <MotionLoop delay={PIN_FIRST} id={id} period={PERIOD} reset={RESET} rest={SETTLED} tracks={sendTracks(feel(tier), shift)}>
      <Stage>
        <Layer name="before" opaque><ChatAt state="before" /></Layer>
        <Layer name="after" seeThrough><ChatAt state="after" /></Layer>
      </Stage>
    </MotionLoop>
  );
}

/** Failure: the message goes back the way it came, the working bubble leaves first, and the text is
 *  back in the composer with Send. */
const FAIL_AT = 2100;

export function FailLoop({ tier }: { tier: 'now' | 'standard' }) {
  if (tier === 'now') {
    return (
      <MotionLoop
        delay={PIN_FIRST}
        id="send-failed-now"
        period={PERIOD}
        rest={FAIL_AT - 100}
        tracks={[{ el: inLayer('before'), beats: [{ at: SEND_AT, to: { opacity: 0 }, curve: cut }, { at: FAIL_AT, to: { opacity: 1 }, curve: cut }] }]}
      >
        <PhoneStage>
          <Layer name="after" opaque><ChatAt state="after" /></Layer>
          <Layer name="before" opaque><ChatAt state="before" /></Layer>
        </PhoneStage>
      </MotionLoop>
    );
  }
  const f = feel('standard');
  const leave = timed('instant', 'exit');
  const back = timed('base', 'move');
  // The working bubble and Stop leave first; then the message sinks back under the composer as the
  // conversation glides down, and the composer shows the text again.
  const sinkAt = FAIL_AT + leave.dur;
  const settled = sinkAt + back.dur;
  const before = (selector: string) => inLayer('before', selector);
  const after = (selector: string) => inLayer('after', selector);
  const undo: Record<string, Track['beats']> = {
    [before(TRANSCRIPT)]: [{ at: settled, to: { opacity: 1 }, curve: cut }],
    [before(HEADER)]: [{ at: settled, to: { opacity: 1 }, curve: cut }],
    // Back under the new composer while it still covers them, so they appear as it fades.
    [before(COMPOSER)]: [{ at: FAIL_AT, to: { opacity: 1 }, curve: cut }],
    [before(SEND)]: [{ at: FAIL_AT, to: { opacity: 1, scale: 1 }, curve: cut }],
    [before(`${COMPOSER} ${TYPED}`)]: [{ at: FAIL_AT, to: { opacity: 1 }, curve: cut }],
    [inLayer('after')]: [{ at: settled, to: { opacity: 0 }, curve: cut }],
    [after(TRANSCRIPT_ROWS)]: [{ at: sinkAt, to: { y: SHIFT.phone.send }, curve: back }],
    [after(row(sent.id))]: [{ at: sinkAt, to: { opacity: 0, y: f.rise, scale: f.grow }, curve: back, fade: timed('base', 'exit') }],
    [after(WORKING)]: [{ at: FAIL_AT, to: { opacity: 0 }, curve: leave }],
    [after(STOP)]: [{ at: FAIL_AT, to: { opacity: 0, scale: f.morph }, curve: leave }],
    [after(`${COMPOSER} ${TYPED}`)]: [{ at: FAIL_AT, to: { opacity: 0 }, curve: leave }],
    [after(COMPOSER)]: [{ at: sinkAt, to: { opacity: 0 }, curve: timed('fast', 'enter') }],
  };
  const tracks: Track[] = sendTracks(f, SHIFT.phone.send).map((track) => ({ ...track, beats: [...track.beats, ...(undo[track.el] ?? [])] }));
  tracks.push({ el: after(`${COMPOSER} ${TYPED}`), beats: undo[after(`${COMPOSER} ${TYPED}`)] });
  return (
    <MotionLoop delay={PIN_FIRST} id="send-failed-standard" period={PERIOD} rest={FAIL_AT - 100} tracks={tracks}>
      <PhoneStage>
        <Layer name="before" opaque><ChatAt state="before" /></Layer>
        <Layer name="after" seeThrough><ChatAt state="after" /></Layer>
      </PhoneStage>
    </MotionLoop>
  );
}


