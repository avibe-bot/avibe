// Going from the Inbox into a conversation and back on a phone (docs/plans/web-ui-motion.md M5): the
// card answers the tap, the conversation slides in over the Inbox, which drifts a little the other way
// (as a native push does), and Back reverses it. Desktop switches conversations in place, so it does not
// slide (see the plan). Implemented with same-document View Transitions where the browser has them.
import { cut, drift, offset, recedeOpacity, scaleOf, spring, timed, type Track } from '../../kit/motion';
import { MotionLoop } from '../../kit/MotionLoop';
import { card, CHAT, inLayer, type Tier } from './fixtures';
import { ChatAt, InboxAt, Layer, PhoneStage } from './stage';

const TAP_AT = 500;
const PUSH_AT = 600;
const BACK_AT = 2600;
const PERIOD = 4400;

function pushTracks(tier: Tier): Track[] {
  const chat = inLayer('chat');
  const inbox = inLayer('inbox');
  const tapped = inLayer('inbox', card(CHAT.replied));
  const press: Track = {
    el: tapped,
    beats: [
      { at: TAP_AT, to: { scale: scaleOf('press') }, curve: timed('instant', 'enter') },
      { at: PUSH_AT + 500, to: { scale: 1 }, curve: cut },
    ],
  };
  if (tier === 'now') {
    return [{ el: chat, from: { opacity: 0 }, beats: [{ at: PUSH_AT, to: { opacity: 1 }, curve: cut }, { at: BACK_AT, to: { opacity: 0 }, curve: cut }] }];
  }
  if (tier === 'reduced') {
    const fade = timed('fast', 'enter');
    return [{ el: chat, from: { opacity: 0 }, beats: [{ at: PUSH_AT, to: { opacity: 1 }, curve: fade }, { at: BACK_AT, to: { opacity: 0 }, curve: fade }] }];
  }
  if (tier === 'restrained') {
    // Fade through: the Inbox leaves before the conversation comes in, so two pages of text never
    // overlap; the conversation drifts in the last `--motion-offset-lg`.
    const shift = offset('lg');
    const leave = timed('instant', 'exit');
    return [
      press,
      {
        el: inbox,
        beats: [
          { at: PUSH_AT, to: { opacity: 0 }, curve: leave },
          { at: BACK_AT + leave.dur, to: { opacity: 1 }, curve: timed('base', 'enter') },
        ],
      },
      {
        el: chat,
        from: { opacity: 0, x: shift },
        beats: [
          { at: PUSH_AT + leave.dur, to: { opacity: 1, x: 0 }, curve: timed('base', 'enter') },
          { at: BACK_AT, to: { opacity: 0, x: shift }, curve: leave },
        ],
      },
    ];
  }
  const slide = spring('sheet');
  const away = tier === 'pronounced' ? { x: drift(), scale: scaleOf('recede'), opacity: recedeOpacity() } : { x: drift() };
  return [
    press,
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
      origin: 'center',
      beats: [
        { at: PUSH_AT, to: away, curve: slide },
        { at: BACK_AT, to: { x: '0%', scale: 1, opacity: 1 }, curve: slide },
      ],
    },
  ];
}

export function NavigationLoop({ tier }: { tier: Tier }) {
  return (
    <MotionLoop id={`navigation-${tier}`} period={PERIOD} rest={PUSH_AT + 1200} tracks={pushTracks(tier)}>
      <PhoneStage>
        <Layer name="inbox" opaque><InboxAt state="after" /></Layer>
        <Layer name="chat"><ChatAt state="replied" /></Layer>
      </PhoneStage>
    </MotionLoop>
  );
}
