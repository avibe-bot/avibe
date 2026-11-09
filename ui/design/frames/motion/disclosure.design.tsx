// Opening and closing what the transcript folds (docs/plans/web-ui-motion.md M3, "Opening and closing"), as
// shipped: the shipped ChatPage, and the reader opening an execution-process chip, a Harness group, or a
// Harness row of its own. The layout changes at once and the scroller holds what was tapped where it was.
// What opened is revealed top to bottom (`--motion-disclose-open-duration`) while the rows below slide down
// with its edge, by transform only; closing folds the edge back to the summary's size as the rows rise with it
// (`--motion-disclose-close-duration`). The summary (the chip's pill, the group's folded card) cross-fades
// with what it stands for instead of following it: closing, it comes in on top during the fold's tail while
// the content fades out under it; opening, it stays until the content is in beneath it. The chip's title moves
// between its pill place and the panel head's, so it is never drawn twice. A chip whose steps still load opens
// straight to as many skeleton rows as it counts. Reduced motion: fades only. Each frame settles open; in the
// canvas, close and open it again to play it.
import { frames } from '../../frame';
import { UNANSWERED } from '../../shell/network';
import { activityGroup, agentSays, CHAT_ID, chatFixtures, chatPhone, chatRoute, message, narration, toolCall, userSays } from '../chat/fixtures';

// ----- An execution-process chip -----

const ask = userSays('把动效令牌接进输入框和消息列表。', 12);
const reply = agentSays('接好了：发送、停止和工作中三个过渡都读同一组令牌。', 9);
const chipChat = {
  session: { title: 'Inbox 卡顿与动效' },
  ui: { show_agent_activity: true },
  messages: [userSays('先按「标准」做，做完开 PR。', 30), agentSays('好，按「标准」档实现。', 28), ask, reply],
};
const steps = [
  narration('先看现在的令牌是怎么读的。', 11),
  toolCall('read_file', { path: 'ui/src/lib/motion.ts' }, 11),
  toolCall('edit_file', { path: 'ui/src/components/workbench/Composer.tsx' }, 10),
  toolCall('run_tests', { suite: 'test:chat-scroll' }, 10),
  narration('三个过渡都改成读令牌，测试通过。', 9),
];
const chip = chatFixtures({ ...chipChat, activity: [activityGroup('grp_tokens', reply, steps, { duration_ms: 42_000 })] });
// Its steps still on their way: the panel opens to their skeleton.
const chipLoading = chatFixtures({
  ...chipChat,
  activity: [activityGroup('grp_tokens', reply, steps, { duration_ms: 42_000 })],
  api: { [`/api/sessions/${CHAT_ID}/activity?group_id=grp_tokens`]: UNANSWERED },
});
const openChip = 'button[title="{t:chat.agentActivity.expand}"] >> nth=0';

// ----- A Harness group, and a Harness row of its own -----

const callback = (id: string, text: string, minutesAgo: number) =>
  message('harness', 'harness', text, minutesAgo, {
    author_name: 'agent_run', author_id: id, native_message_id: `agent_run:${id}:${minutesAgo}`,
    source_session_id: 'sesv7t2q9m4kd', source_session_title: 'Vault sandbox Taproot 签名', source_session_agent_name: 'codex',
  });
const group = chatFixtures({
  session: { title: '修复 Vault Taproot 签名' },
  messages: [
    agentSays('签名边界和 sandbox 构建交给子任务，完成后我来验收。', 50),
    callback('run_v1', 'Implementation incorporates shared packaged JSON schema validation for every signing path.', 45),
    callback('run_v2', 'Progress: both source boundaries implemented; 27 focused tests pass and the browser suite is next.', 25),
    callback('run_v3', 'Spot-check of the 15:42 snapshot: one P1, three P2 findings, all with failing tests attached.', 5),
  ],
});
const openGroup = 'button:has-text("{t:chat.harnessGroup.expand}")';
const single = chatFixtures({
  session: { title: '修复 Vault Taproot 签名' },
  messages: [
    agentSays('签名边界交给子任务，完成后我来验收。', 50),
    callback('run_v1', 'Progress: both source boundaries implemented; 27 focused tests pass and the browser suite is next.', 25),
    agentSays('收到，等它跑完浏览器测试再验收。', 20),
  ],
});
const openRow = 'button[aria-expanded="false"]:has-text("Progress: both source boundaries")';

const phone = { width: 390, height: 844, status: 'shipped' as const, route: chatRoute(), render: chatPhone, wait: 600 };

const designFrames = frames([
  { ...phone, name: 'phone-chip', title: '展开执行过程：面板自上而下显现，下方消息随边缘让位，chip 原地不动 · 手机', fixtures: chip, click: [openChip] },
  { ...phone, name: 'phone-chip-loading', title: '展开仍在读取步骤的执行过程：直接展开到同样多行的骨架 · 手机', fixtures: chipLoading, click: [openChip] },
  { ...phone, name: 'phone-group', title: '展开 Harness 消息组：成员自上而下显现，组卡片淡出 · 手机', fixtures: group, click: [openGroup] },
  { ...phone, name: 'phone-harness-row', title: '展开单条 Harness 消息：正文自上而下显现 · 手机', fixtures: single, click: [openRow] },
  { ...phone, name: 'phone-group-reduced', title: '展开 Harness 消息组 · 减少动态效果：只淡入淡出 · 手机', fixtures: group, click: [openGroup], reducedMotion: true },
]);

export default designFrames;
