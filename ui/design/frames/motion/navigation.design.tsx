// Into a conversation from the Inbox and back, on a phone (docs/plans/web-ui-motion.md M5), as shipped: the
// router runs a view transition and plays these keyframes (src/index.css, `data-nav-motion`) on snapshots of
// the two pages. The conversation is pushed in from the right while the Inbox drifts the other way, and our
// own Back reverses it, at a native navigation's pace (`--motion-nav-*`); a system edge swipe runs no
// transition of ours. Under reduced motion the conversation cross-fades in. Here the same keyframes play on
// the two shipped pages, stacked; each frame settles where its step ends. On iOS 17, which has no View
// Transitions, navigation stays a cut.
import type { CSSProperties, ReactNode } from 'react';

import { frames } from '../../frame';
import { ChatAt, InboxAt, Layer, PhoneStage } from '../../kit/motion/Stage';
import { CHAT, inboxFixtures } from './fixtures';

/** When in its loop the step starts, and how often the canvas plays it again. */
const AT = 600;
const REPLAY = 2400;

/** One of the shipped navigation keyframes, on the timing its `data-nav-motion` rule gives it. */
const navStep = (name: string, kind: 'push' | 'pop'): CSSProperties => ({ animation: `${name} var(--motion-nav-${kind}-duration) var(--motion-nav-ease) ${AT}ms both` });
const fade = (name: string): CSSProperties => ({ animation: `${name} var(--motion-duration-fast) var(--motion-ease-move) ${AT}ms both` });

/** A page of the stack, moving as its snapshot does. */
const moving = (style: CSSProperties, page: ReactNode) => <div className="absolute inset-0" style={style}>{page}</div>;

const inbox = <Layer name="inbox" opaque><InboxAt state="after" /></Layer>;
const chat = <Layer name="chat"><ChatAt session={CHAT.replied} /></Layer>;

const phone = { width: 390, height: 844, status: 'shipped' as const, route: '/inbox', fixtures: inboxFixtures('replied'), replay: REPLAY };

const designFrames = frames([
  {
    ...phone,
    name: 'phone-push',
    title: '从收件箱进入会话 · 手机',
    render: () => (
      <PhoneStage>
        {moving(navStep('motion-nav-recede', 'push'), inbox)}
        {moving(navStep('motion-nav-arrive', 'push'), chat)}
      </PhoneStage>
    ),
  },
  {
    ...phone,
    name: 'phone-pop',
    title: '从会话返回收件箱 · 手机',
    render: () => (
      <PhoneStage>
        {moving(navStep('motion-nav-return', 'pop'), inbox)}
        {moving(navStep('motion-nav-depart', 'pop'), chat)}
      </PhoneStage>
    ),
  },
  {
    ...phone,
    name: 'phone-push-reduced',
    title: '进入会话 · 减少动态效果 · 手机',
    reducedMotion: true,
    render: () => (
      <PhoneStage>
        {/* The product sums the two snapshots as they cross; an opaque page fading in over the
            still one shows the same. */}
        {inbox}
        {moving(fade('motion-fade-in'), chat)}
      </PhoneStage>
    ),
  },
]);

export default designFrames;
