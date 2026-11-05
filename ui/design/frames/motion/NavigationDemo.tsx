// Going from the Inbox into a conversation and back on a phone (docs/plans/web-ui-motion.md M5): the
// card answers the tap, the conversation slides in over the Inbox, which drifts a little the other way
// (as a native push does), and Back reverses it. Desktop switches conversations in place, so it does not
// slide (see the plan). Implemented with same-document View Transitions where the browser has them.
import { cut, drift, scaleOf, spring, timed, type Track } from '../../kit/motion';
import { MotionLoop } from '../../kit/MotionLoop';
import { card, CHAT, inLayer } from './fixtures';
import { ChatAt, InboxAt, Layer, PhoneStage } from './stage';

const TAP_AT = 500;
const PUSH_AT = 600;
const BACK_AT = 2600;
const PERIOD = 4400;

function pushTracks(reduced: boolean): Track[] {
  const chat = inLayer('chat');
  const inbox = inLayer('inbox');
  if (reduced) {
    // No travel and no scale: the conversation fades in over the Inbox and out again.
    const fade = timed('fast', 'enter');
    return [{ el: chat, from: { opacity: 0 }, beats: [{ at: PUSH_AT, to: { opacity: 1 }, curve: fade }, { at: BACK_AT, to: { opacity: 0 }, curve: fade }] }];
  }
  const slide = spring('sheet');
  return [
    {
      el: inLayer('inbox', card(CHAT.replied)),
      beats: [
        { at: TAP_AT, to: { scale: scaleOf('press') }, curve: timed('instant', 'enter') },
        { at: PUSH_AT + 500, to: { scale: 1 }, curve: cut },
      ],
    },
    {
      el: chat,
      from: { x: '100%' },
      beats: [
        { at: PUSH_AT, to: { x: '0%' }, curve: slide },
        { at: BACK_AT, to: { x: '100%' }, curve: slide },
      ],
    },
    {
      el: inbox,
      beats: [
        { at: PUSH_AT, to: { x: drift() }, curve: slide },
        { at: BACK_AT, to: { x: '0%' }, curve: slide },
      ],
    },
  ];
}

export function NavigationLoop({ reduced = false }: { reduced?: boolean }) {
  return (
    <MotionLoop id={`navigation${reduced ? '-reduced' : ''}`} period={PERIOD} rest={PUSH_AT + 1200} tracks={pushTracks(reduced)}>
      <PhoneStage>
        <Layer name="inbox" opaque><InboxAt state="after" /></Layer>
        <Layer name="chat"><ChatAt state="replied" /></Layer>
      </PhoneStage>
    </MotionLoop>
  );
}
