// Vibey's hosted tool calls in chat (HostedToolRow), inside the execution process the shipped ChatPage draws:
// web search and X search (run natively, by proxy, or moved to another Source), the search services, web
// fetch, and image generation. The cards are the producer's pinned payloads, `src/lib/hosted*.fixture.json`.
import blogHeaderUrl from '../../assets/blog-header-night.jpg';
import { frames } from '../../frame';
import hosted from '@/lib/hostedActivity.fixture.json';
import fetches from '@/lib/hostedFetchActivity.fixture.json';
import services from '@/lib/hostedServiceActivity.fixture.json';

import { activityGroup, agentSays, chatDesktop, chatFixtures, chatPhone, chatRoute, hostedCall, userSays } from './fixtures';

const ui = { show_agent_activity: true };
const openChip = 'button[title="{t:chat.agentActivity.expand}"] >> nth=0';
const openCall = (index: number) => `button[title="{t:chat.agentActivity.showCall}"] >> nth=${index}`;

// ----- Web search and X search -----

const ask1 = userSays('OpenAI 这周在 X 上发了什么？和他们官网的公告对得上吗？', 40);
const reply1 = agentSays(
  '这周 @OpenAI 在 X 上发了两条值得注意的帖子，都能在官网找到对应公告：\n\n- DevDay 2026 回放与发布清单，官网博客同日发布 [openai.com](https://openai.com/index/devday-2026)\n- Responses API 网页搜索支持域名过滤，开发者更新日志一致 [platform.openai.com](https://platform.openai.com/docs/changelog)\n\n结论：两条都有官网出处，可以直接引用。',
  38,
  {
    content: {
      citations: [
        { index: 1, url: 'https://openai.com/index/devday-2026', label: 'openai.com', title: 'DevDay 2026 recap' },
        { index: 2, url: 'https://platform.openai.com/docs/changelog', label: 'platform.openai.com', title: 'Responses API changelog' },
      ],
    },
  },
);
const ask2 = userSays('那 Anthropic 这周有什么新发布？', 12);
const reply2 = agentSays('这周 Anthropic 有一条正式公告，已附来源 [openai.com](https://openai.com/index/devday-2026)。', 10, {
  content: { citations: [{ index: 1, url: 'https://openai.com/index/devday-2026', label: 'openai.com', title: 'DevDay 2026 recap' }] },
});

const vibey = { title: 'OpenAI 本周动态核对' };
const search = chatFixtures({
  session: vibey,
  ui,
  messages: [ask1, reply1, ask2, reply2],
  activity: [
    activityGroup('grp_search', reply1, [hostedCall(hosted.native_web_search, 39.8), hostedCall(hosted.proxy_x_search, 39.5)], { duration_ms: 14_000 }),
    activityGroup('grp_fallback', reply2, [hostedCall(hosted.proxy_fallback, 11.6)], { duration_ms: 21_000 }),
  ],
});

// ----- Every way a search call can end -----

const askStates = userSays('查一下 DeepSeek 的搜索 API 怎么收费。', 20);
const replyStates = agentSays('DeepSeek 搜索按次计费，结果数量不影响单价。', 15);
const states = chatFixtures({
  session: { title: '搜索服务' },
  ui,
  messages: [askStates, replyStates],
  activity: [
    activityGroup(
      'grp_states',
      replyStates,
      [
        services.service_completed.payload,
        services.service_switch.payload,
        services.service_estimate.payload,
        services.service_blocked.payload,
        hosted.proxy_retried,
        hosted.native_interrupted,
        hosted.proxy_failed,
        hosted.proxy_unreachable,
      ].map((card, index) => hostedCall(card, 19.9 - index * 0.2)),
      { duration_ms: 32_000 },
    ),
  ],
});

// ----- Web fetch -----

const askFetch = userSays('把这几个页面读一下，整理价格和接口说明。', 20);
const replyFetch = agentSays('读完了：五个页面读到了，内网地址那个被本机策略拒绝，没有转给任何第三方。', 15);
const fetchFixture = chatFixtures({
  session: { title: '网页读取' },
  ui,
  messages: [askFetch, replyFetch],
  activity: [
    activityGroup(
      'grp_fetch',
      replyFetch,
      [fetches.local_truncated, fetches.jina_fallback, fetches.tavily, fetches.exa, fetches.challenge, fetches.policy, fetches.degraded].map(
        ({ activity }, index) => hostedCall(activity, 19.9 - index * 0.2),
      ),
      { duration_ms: 26_000 },
    ),
  ],
});

// ----- Image generation -----

const image = {
  ...hosted.direct_image,
  images: hosted.direct_image.images.map((file) => ({ ...file, path: '/Users/demo/avibe/_tmp/blog-header-night.png', media_url: '/api/media/tok_blog' })),
};
const askImage = userSays('给这周的发布博客画一张头图：深夜书桌上的一台笔记本，暗调，留出放标题的位置，横版。', 20);
const replyImage = agentSays(
  '画好了：暗调书桌上的一台笔记本，台灯斜光落在键盘上，横版 1536×1024，右上留了放标题的暗部。\n\n![blog-header-night.png](/api/media/tok_blog?w=1536&h=1024)\n\n想要更亮一些或换成插画风，告诉我就重画。',
  18,
);
const imageFixture = chatFixtures({
  session: { title: '发布博客头图', model: 'claude-opus-5' },
  ui,
  messages: [askImage, replyImage],
  activity: [activityGroup('grp_image', replyImage, [hostedCall(image, 19.5)], { duration_ms: 52_000 })],
  api: { '/api/media/tok_blog': blogHeaderUrl },
});

const designFrames = frames([
  {
    name: 'search',
    title: '托管工具 · 网页搜索与 X 搜索（原生、代理、自动改用）',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: chatRoute(),
    fixtures: search,
    click: [openChip, openChip],
    render: chatDesktop,
  },
  {
    name: 'search-sources',
    title: '托管工具 · 展开 X 搜索的来源',
    width: 1192,
    height: 960,
    status: 'shipped',
    route: chatRoute(),
    fixtures: search,
    click: [openChip, openCall(1)],
    render: chatDesktop,
  },
  {
    name: 'search-states',
    title: '托管工具 · 搜索服务、改用、被拦截、中断与失败',
    width: 1192,
    height: 1000,
    status: 'shipped',
    route: chatRoute(),
    fixtures: states,
    click: [openChip],
    render: chatDesktop,
  },
  {
    name: 'fetch',
    title: '托管工具 · 网页读取（本机、免费服务、改用、验证页与策略拒绝）',
    width: 1192,
    height: 980,
    status: 'shipped',
    route: chatRoute(),
    fixtures: fetchFixture,
    click: [openChip],
    render: chatDesktop,
  },
  {
    name: 'fetch-detail',
    title: '托管工具 · 展开一次网页读取（执行者、大小与截断）',
    width: 1192,
    height: 1240,
    status: 'shipped',
    route: chatRoute(),
    fixtures: fetchFixture,
    click: [openChip, openCall(0)],
    render: chatDesktop,
  },
  {
    name: 'image',
    title: '托管工具 · 图片生成（指定模型）',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: chatRoute(),
    fixtures: imageFixture,
    click: [openChip],
    render: chatDesktop,
  },
  {
    name: 'image-detail',
    title: '托管工具 · 展开图片生成（文件与提示词）',
    width: 1192,
    height: 1140,
    status: 'shipped',
    route: chatRoute(),
    fixtures: imageFixture,
    click: [openChip, openCall(0)],
    render: chatDesktop,
  },
  {
    name: 'phone-search',
    title: '托管工具 · 网页搜索与 X 搜索 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: search,
    click: [openChip],
    render: chatPhone,
  },
]);

export default designFrames;
