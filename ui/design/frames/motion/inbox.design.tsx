// The Inbox when a reply lands in a conversation lower down (docs/plans/web-ui-motion.md M7, and M4 for a
// cached list replaced by the live one), shipped on the Inbox page: the card leaves its slot, the cards
// above glide down, and it comes in at the top. The shipped page plays it as the Inbox state changes
// (`StateLoop`); the screenshot shows where it settles.
//
// A finger swiping an unread card sideways to mark it read (M7, `useCardSwipe`), shipped: the card follows
// the finger either way, and the side it uncovers shows 「Read」 a small gap behind its edge, soft mint
// until letting go would mark it read (past 30% of the card's width), then filled with its icon grown.
// Let go short of that, it springs back on `spring-snappy`; past it, or flicked, the card returns to rest
// and the read change plays (the count shrinks, the wash eases away, the Unread tab lets the card go).
import { InboxPage } from '@/components/workbench/InboxPage';

import { frames } from '../../frame';
import { InboxAt } from '../../kit/motion/Stage';
import { InboxStateProvider } from '../../shell/DesignShell';
import { PhoneScreen } from '../../shell/PhoneScreen';
import { StateLoop } from '../../shell/StateLoop';
import { TouchPoint } from '../../shell/TouchPoint';
import { inboxFixtures } from './fixtures';

/** The list as it was, then the reply lands; the loop holds the new order and starts over. */
const STEPS = [{ at: 0, state: 'before' }, { at: 900, state: 'after' }] as const;
const PERIOD = 4400;

const move = (reduced: boolean) => () => (
  <StateLoop steps={STEPS} period={PERIOD} reduced={reduced}>
    {(state) => (
      <InboxStateProvider state={state}>
        <PhoneScreen>
          <InboxPage onOpenSearch={() => {}} />
        </PhoneScreen>
      </InboxStateProvider>
    )}
  </StateLoop>
);

const phone = { width: 390, height: 844, status: 'shipped' as const, route: '/inbox', fixtures: inboxFixtures() };

/** The first two cards at 390 × 844 (the shipped page with the `before` Inbox), where a finger holds each. */
const CARDS = {
  cache: { id: 'ses_demo_cache', fingerY: 296 },
  pr: { id: 'ses_demo_pr', fingerY: 476 },
} as const;
/** 30% of a card's width there (358 px): past it, letting go marks the card read. */
const SHORT = 72;
const PAST = 150;

/** A card `offset` px across, posed as `useCardSwipe` writes it (the card, and the side it uncovers), with
 *  the action filled when `armed`. The finger started 40 px in from the edge it moves away from. */
const swiped = (card: keyof typeof CARDS, offset: number, armed: boolean) => {
  const { id, fingerY } = CARDS[card];
  const row = `[data-flip-id="${id}"]`;
  return () => (
    <>
      <InboxAt state="before" />
      <style>{`
        ${row} > div:has(> article) { transform: translateX(${offset}px); }
        ${row} [data-swipe-side="start"] { transform: translateX(calc(${Math.max(offset, 0)}px - 100%)); }
        ${row} [data-swipe-side="end"] { transform: translateX(calc(100% + ${Math.min(offset, 0)}px)); }
        ${armed ? `${row} .swipe-action { --swipe-armed: 1; }` : ''}
      `}</style>
      <TouchPoint top={fingerY} left={(offset < 0 ? 334 : 56) + offset} />
    </>
  );
};

const designFrames = frames([
  { ...phone, wait: 1800, name: 'phone-move', title: '收件箱 · 新回复把会话移到最上面 · 手机', render: move(false) },
  { ...phone, wait: 1800, name: 'phone-move-reduced', title: '收件箱 · 移到最上面 · 减少动态效果 · 手机', render: move(true) },
  { ...phone, name: 'phone-swipe-left', title: '收件箱 · 左滑标记已读 · 未过 30%，松手回弹 · 手机', render: swiped('cache', -SHORT, false) },
  { ...phone, name: 'phone-swipe-left-armed', title: '收件箱 · 左滑标记已读 · 已过 30%，松手即已读 · 手机', render: swiped('cache', -PAST, true) },
  { ...phone, name: 'phone-swipe-right', title: '收件箱 · 右滑标记已读 · 未过 30%，松手回弹 · 手机', render: swiped('pr', SHORT, false) },
  { ...phone, name: 'phone-swipe-right-armed', title: '收件箱 · 右滑标记已读 · 已过 30%，松手即已读 · 手机', render: swiped('pr', PAST, true) },
]);

export default designFrames;
