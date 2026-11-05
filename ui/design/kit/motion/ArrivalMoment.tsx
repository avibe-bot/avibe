// A reply arriving (docs/plans/web-ui-motion.md M3, M4): the working bubble gives way to the reply in
// the same place, the conversation glides up by what the reply adds, and the composer hands Stop back to
// Send. The same motion carries a conversation painted from the local cache when the live answer brings
// rows it did not have: a row that is new rises in, nothing that was already there moves on its own,
// and nothing pops.
import { cut, scaleOf, spring, timed, type Track } from './loop';
import { MotionLoop } from './MotionLoop';
import type { ChatCast } from './cast';
import { COMPOSER, HEADER, inLayer, row, SEND, STOP, TRANSCRIPT_ROWS, TYPED, WORKING } from './selectors';
import { ChatAt, Layer, PhoneStage } from './Stage';

const ARRIVE_AT = 800;
const PERIOD = 3600;
const RESET = 500;
/** The page pins its transcript to the bottom when its data lands; the loop starts after that. */
const PIN_FIRST = 1500;

function arrivalTracks(shift: number, replyId: string): Track[] {
  const before = (selector: string) => inLayer('after', selector);
  const after = (selector: string) => inLayer('replied', selector);
  const leave = timed('instant', 'exit');
  const glide = spring('smooth');
  return [
    // Every row but the working bubble hands over at once (the replied page shows them in the same
    // place); the bubble stays and fades as the reply grows over it.
    { el: before(`${TRANSCRIPT_ROWS} > :not(:last-child)`), beats: [{ at: ARRIVE_AT, to: { opacity: 0 }, curve: cut }] },
    { el: before(WORKING), beats: [{ at: ARRIVE_AT, to: { opacity: 0 }, curve: leave }] },
    { el: before(HEADER), beats: [{ at: ARRIVE_AT, to: { opacity: 0 }, curve: cut }] },
    { el: before(`${COMPOSER} ${TYPED}`), beats: [{ at: ARRIVE_AT, to: { opacity: 0 }, curve: leave }] },
    { el: before(STOP), beats: [{ at: ARRIVE_AT, to: { opacity: 0, scale: scaleOf('morph') }, curve: leave }] },
    { el: before(COMPOSER), beats: [{ at: ARRIVE_AT + leave.dur + timed('fast', 'enter').dur, to: { opacity: 0 }, curve: cut }] },
    { el: inLayer('replied'), from: { opacity: 0 }, beats: [{ at: ARRIVE_AT, to: { opacity: 1 }, curve: cut }] },
    {
      el: after(TRANSCRIPT_ROWS),
      beats: [
        { at: ARRIVE_AT - 1, to: { y: shift }, curve: cut },
        { at: ARRIVE_AT, to: { y: 0 }, curve: glide },
      ],
    },
    {
      el: after(row(replyId)),
      origin: 'top left',
      from: { opacity: 0, scale: scaleOf('enter') },
      beats: [{ at: ARRIVE_AT, to: { opacity: 1, scale: 1 }, curve: glide, fade: timed('base', 'enter') }],
    },
    { el: after(COMPOSER), from: { opacity: 0 }, beats: [{ at: ARRIVE_AT + leave.dur, to: { opacity: 1 }, curve: timed('fast', 'enter') }] },
    { el: after(SEND), from: { scale: scaleOf('morph') }, beats: [{ at: ARRIVE_AT + leave.dur, to: { scale: 1 }, curve: spring('snappy') }] },
  ];
}

export function ArrivalLoop({ cast, shift, reduced = false }: { cast: ChatCast; shift: number; reduced?: boolean }) {
  if (!reduced) {
    return (
      <MotionLoop delay={PIN_FIRST} id="arrival" period={PERIOD} reset={RESET} rest={ARRIVE_AT + 1200} tracks={arrivalTracks(shift, cast.replyId)}>
        <PhoneStage>
          <Layer name="after" opaque><ChatAt session={cast.after} /></Layer>
          <Layer name="replied" seeThrough><ChatAt session={cast.replied} /></Layer>
        </PhoneStage>
      </MotionLoop>
    );
  }
  // With reduced motion the pages cross-fade.
  return (
    <MotionLoop
      delay={PIN_FIRST}
      id="arrival-reduced"
      period={PERIOD}
      reset={RESET}
      rest={ARRIVE_AT + 1200}
      tracks={[{ el: inLayer('after'), beats: [{ at: ARRIVE_AT, to: { opacity: 0 }, curve: timed('fast', 'enter') }] }]}
    >
      <PhoneStage>
        <Layer name="replied" opaque><ChatAt session={cast.replied} /></Layer>
        <Layer name="after" opaque><ChatAt session={cast.after} /></Layer>
      </PhoneStage>
    </MotionLoop>
  );
}
