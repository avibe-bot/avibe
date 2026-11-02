// Show Page annotations in chat (批注消息), rendered by the shipped ChatPage. One message type, two
// directions: the user's annotation sits on the right, the agent's on the left, each titled by who made it.
// The anchor quote sits above the body, a boxed region shows as its screenshot, and only a resolved mark
// carries 已处理. A queued annotation lives in the queue strip alone until its turn comes.
import regionUrl from '../../assets/annotation-region.svg';
import { frames } from '../../frame';
import { agentSays, chatDesktop, chatFixtures, chatPhone, chatRoute, message, runningTurn, userSays } from './fixtures';

type Annotation = { direction: 'user' | 'agent'; action: 'created' | 'resolved'; quote?: string };

const fromShowPage = (event: string) => ({
  author_name: 'show_annotation',
  author_id: event,
  native_message_id: `show:${event}`,
  metadata: { source: 'show_page', show_event_id: event, show_event_type: 'human.annotation.created', show_event_scope: 'default' },
});

/** The user's annotation on the page: turn input written by the Show runtime, so authored by `harness`. */
const userMarks = (text: string, annotation: Omit<Annotation, 'direction'>, minutesAgo: number, attachments?: unknown[]) =>
  message('harness', 'annotation', text, minutesAgo, {
    ...fromShowPage(`evt_${minutesAgo}`),
    content: { text, annotation: { direction: 'user', ...annotation }, ...(attachments ? { attachments } : {}) },
  });

/** The agent's mark back on the page. */
const agentMarks = (text: string, annotation: Omit<Annotation, 'direction'>, minutesAgo: number) =>
  message('agent', 'annotation', text, minutesAgo, { source: null, content: { text, annotation: { direction: 'agent', ...annotation } } });

const region = { url: '/api/media/tok_region', name: 'annotation-region.png', mime: 'image/png', kind: 'image', width: 640, height: 300 };

const messages = [
  userSays('Model Hub 那一屏的层级不太对，我直接在页面上标给你。', 48),
  agentSays('好，收到。我打开 Show Page 看一下，你标完我按标注逐条改。', 47),
  userMarks('这里的标题太小了，正文和它几乎一样重。', { action: 'created', quote: 'Model Hub' }, 44),
  agentMarks('标题已经改成 20px/600，正文降到 14px，层级现在拉开了。', { action: 'created', quote: 'Model Hub' }, 39),
  userMarks('这两个卡片的间距不一致，我截了一块给你。', { action: 'created' }, 36, [region]),
  agentMarks('间距统一到 16px 了。', { action: 'resolved' }, 30),
];

// While the agent works on the last mark, the next one waits in the queue strip, and only there.
const queued = [
  message('harness', 'queued', '图例颜色和柱子对不上，紫色应该是 IM。', 2, {
    ...fromShowPage('evt_queued'),
    state: 'queued',
    content: { text: '图例颜色和柱子对不上，紫色应该是 IM。', annotation: { direction: 'user', action: 'created', quote: 'Weekly pass trend' } },
  }),
];

const fixtures = chatFixtures({
  session: { title: 'Model Hub 视觉走查' },
  messages,
  queued,
  turnState: runningTurn,
  api: { '/api/media/tok_region': regionUrl },
});

const designFrames = frames([
  {
    name: 'default',
    title: '批注消息 · 用户批注在右，Agent 批注在左，排队中的批注只在排队条',
    width: 1192,
    height: 1260,
    status: 'shipped',
    route: chatRoute(),
    fixtures,
    render: chatDesktop,
  },
  {
    name: 'phone',
    title: '批注消息 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures,
    render: chatPhone,
  },
]);

export default designFrames;
