// Going from the Inbox into a conversation and back on a phone (docs/plans/web-ui-motion.md M5): the
// card answers the tap, the conversation slides in over the Inbox, which drifts a little the other way
// (as a native push does), and Back reverses it. Desktop switches conversations in place, so it does not
// slide (see the plan). Implemented with same-document View Transitions where the browser has them.
import { cut, offset, scaleFrom, spring, timed, type Track } from '../../kit/motion';
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
      { at: TAP_AT, to: { scale: scaleFrom('press') }, curve: timed('instant', 'enter') },
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
    const shift = offset('lg');
    return [
      press,
      {
        el: chat,
        from: { opacity: 0, x: shift },
        beats: [
          { at: PUSH_AT, to: { opacity: 1, x: 0 }, curve: timed('base', 'enter') },
          { at: BACK_AT, to: { opacity: 0, x: shift }, curve: timed('fast', 'exit') },
        ],
      },
    ];
  }
  const slide = spring('sheet');
  const drift = tier === 'pronounced' ? { x: '-30%' as const, scale: 0.94, opacity: 0.6 } : { x: '-30%' as const };
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
        { at: PUSH_AT, to: drift, curve: slide },
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
