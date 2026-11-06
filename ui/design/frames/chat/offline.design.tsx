// A conversation read from what this device saved, because the server cannot be reached
// (docs/plans/local-sync-store.md, decision D4: one quiet connection notice, and a marker where
// saved history ends; no per-conversation badge). Drawn with the shipped pieces in the chat's
// column: the notice the chat shows above its transcript, and the marker in the head slot of the
// transcript, at the slot's fixed height, where the oldest saved message follows. The chat around them
// is the shipped ChatPage; the design canvas cannot fail its bootstrap or seed the device's store, so
// these frames draw the two pieces alone.
import { ChatOfflineNotice, HistoryGapMarker } from '@/components/workbench/ChatOfflineState';

import { frames } from '../../frame';

const noop = () => {};


const designFrames = frames([
  {
    name: 'notice',
    title: '会话 · 离线 · 显示本机保存的内容',
    width: 760,
    height: 120,
    status: 'shipped',
    render: () => (
      <div className="px-4">
        <ChatOfflineNotice />
      </div>
    ),
  },
  {
    name: 'history-gap',
    title: '会话 · 离线 · 本机保存的历史到此为止',
    width: 760,
    height: 100,
    status: 'shipped',
    render: () => (
      <div className="mx-auto w-full max-w-[1080px] px-4 pt-4">
        <HistoryGapMarker onRetry={noop} />
      </div>
    ),
  },
  {
    name: 'phone',
    title: '会话 · 离线 · 手机',
    width: 390,
    height: 140,
    status: 'shipped',
    render: () => (
      <div className="flex flex-col gap-3 px-3">
        <ChatOfflineNotice />
        <HistoryGapMarker onRetry={noop} />
      </div>
    ),
  },
]);

export default designFrames;
