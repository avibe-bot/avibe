// A reply moves its conversation to the top of the Inbox (docs/plans/web-ui-motion.md M7): the card leaves
// its old slot, the cards above it glide down into the room, and it comes in at the top with the new
// reply. The cards are tinted, not opaque, so one never travels across the others. The same motion
// carries a cached Inbox when the live list lands in another order (M4). Nothing changes height.
import { beat, cut, offset, scaleOf, spring, timed, type Curve, type Track } from '../../kit/motion';
import { MotionLoop } from '../../kit/MotionLoop';
import { card, CHAT, inLayer, INBOX_FLIP } from './fixtures';
import { InboxAt, Layer, PhoneStage } from './stage';

const MOVE_AT = 900;
const PERIOD = 3600;
const RESET = 500;

/** `closeUp`: the list closes up once the card has left its slot (its `instant` fade); `enterAfter`: the
 *  card comes in at the top a beat after the cards below have made most of the room. */
type Feel = { glide: Curve; enter: Curve; from: { y: number; scale: number }; leaveScale: number; closeUp: number; enterAfter: number };

/** The approved (standard) move, from the tokens. */
function feel(): Feel {
  const closeUp = timed('instant', 'enter').dur;
  const grow = scaleOf('enter-sm');
  return {
    glide: spring('smooth'),
    enter: spring('smooth'),
    from: { y: -offset('md'), scale: grow },
    leaveScale: grow,
    closeUp,
    enterAfter: closeUp + timed('fast', 'enter').dur + beat() / 2,
  };
}

function moveTracks(f: Feel): Track[] {
  const moved = card(CHAT.replied);
  const tracks: Track[] = [
    { el: inLayer('after'), from: { opacity: 0 }, beats: [{ at: MOVE_AT, to: { opacity: 1 }, curve: cut }] },
    // The card as it was, alone over the new list, leaving its old slot.
    { el: inLayer('ghost'), visible: false, from: { opacity: 0 }, beats: [{ at: MOVE_AT, to: { opacity: 1 }, curve: cut }] },
    {
      el: inLayer('ghost', moved),
      visible: true,
      // Gone before the cards close up over its slot, so no two cards' text ever overlaps.
      beats: [{ at: MOVE_AT, to: { opacity: 0, scale: f.leaveScale }, curve: timed('instant', 'enter') }],
    },
    // The tab bar again over that card, so it leaves under the bar as the list does.
    { el: inLayer('chrome'), visible: false, from: { opacity: 0 }, beats: [{ at: MOVE_AT, to: { opacity: 1 }, curve: cut }] },
    { el: inLayer('chrome', 'nav'), visible: true, beats: [] },
    {
      el: inLayer('after', moved),
      origin: 'top center',
      from: { opacity: 0, ...f.from },
      beats: [{ at: MOVE_AT + f.enterAfter, to: { opacity: 1, y: 0, scale: 1 }, curve: f.enter, fade: timed('base', 'enter') }],
    },
  ];
  for (const [sessionId, from] of Object.entries(INBOX_FLIP)) {
    if (sessionId === CHAT.replied) continue;
    tracks.push({
      el: inLayer('after', card(sessionId)),
      beats: [
        // FLIP: each card takes the offset back to its old slot only as the list changes.
        { at: MOVE_AT - 1, to: { y: from }, curve: cut },
        { at: MOVE_AT + f.closeUp, to: { y: 0 }, curve: f.glide },
      ],
    });
  }
  return tracks;
}

export function InboxMoveLoop({ reduced = false }: { reduced?: boolean }) {
  if (reduced) {
    return (
      <MotionLoop
        id="inbox-reduced"
        period={PERIOD}
        reset={RESET}
        rest={MOVE_AT + 1200}
        tracks={[{ el: inLayer('before'), beats: [{ at: MOVE_AT, to: { opacity: 0 }, curve: timed('fast', 'enter') }] }]}
      >
        <PhoneStage>
          <Layer name="after" opaque><InboxAt state="after" /></Layer>
          <Layer name="before" opaque><InboxAt state="before" /></Layer>
        </PhoneStage>
      </MotionLoop>
    );
  }
  return (
    <MotionLoop id="inbox" period={PERIOD} reset={RESET} rest={MOVE_AT + 1200} tracks={moveTracks(feel())}>
      <PhoneStage>
        <Layer name="before" opaque><InboxAt state="before" /></Layer>
        <Layer name="after" opaque><InboxAt state="after" /></Layer>
        <Layer name="ghost"><InboxAt state="before" /></Layer>
        <Layer name="chrome"><InboxAt state="after" /></Layer>
      </PhoneStage>
    </MotionLoop>
  );
}
