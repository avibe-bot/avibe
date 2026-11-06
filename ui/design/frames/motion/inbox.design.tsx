// The Inbox when a reply lands in a conversation lower down (docs/plans/web-ui-motion.md M7, and M4 for a
// cached list replaced by the live one), shipped on the Inbox page: the card leaves its slot, the cards
// above glide down, and it comes in at the top. The shipped page plays it as the Inbox state changes
// (`StateLoop`); the screenshot shows where it settles.
import { InboxPage } from '@/components/workbench/InboxPage';

import { frames } from '../../frame';
import { InboxStateProvider } from '../../shell/DesignShell';
import { PhoneScreen } from '../../shell/PhoneScreen';
import { StateLoop } from '../../shell/StateLoop';
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

const phone = { width: 390, height: 844, status: 'shipped' as const, route: '/inbox', fixtures: inboxFixtures(), wait: 1800 };

const designFrames = frames([
  { ...phone, name: 'phone-move', title: '收件箱 · 新回复把会话移到最上面 · 手机', render: move(false) },
  { ...phone, name: 'phone-move-reduced', title: '收件箱 · 移到最上面 · 减少动态效果 · 手机', render: move(true) },
]);

export default designFrames;
