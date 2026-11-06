// Opening what the transcript folds (docs/plans/web-ui-motion.md M3, "Opening and closing"), as shipped:
// the shipped ChatPage, and the reader opening an execution-process chip or a Harness group. The layout
// changes at once and the scroller holds what was tapped where it was; what opened shows itself from its
// top-left corner (`--motion-scale-enter-sm`, fading in on `fast`), only by what is inside the item.
// Closing is a cut. Each frame settles open; in the canvas, close and open it again to play it.
import { frames } from '../../frame';
import { activityGroup, agentSays, chatFixtures, chatPhone, chatRoute, message, narration, toolCall, userSays } from '../chat/fixtures';

// ----- An execution-process chip -----

const ask = userSays('把动效令牌接进输入框和消息列表。', 12);
const reply = agentSays('接好了：发送、停止和工作中三个过渡都读同一组令牌。', 9);
const chip = chatFixtures({
  session: { title: 'Inbox 卡顿与动效' },
  ui: { show_agent_activity: true },
  messages: [userSays('先按「标准」做，做完开 PR。', 30), agentSays('好，按「标准」档实现。', 28), ask, reply],
  activity: [
    activityGroup('grp_tokens', reply, [
      narration('先看现在的令牌是怎么读的。', 11),
      toolCall('read_file', { path: 'ui/src/lib/motion.ts' }, 11),
      toolCall('edit_file', { path: 'ui/src/components/workbench/Composer.tsx' }, 10),
    ], { duration_ms: 42_000 }),
  ],
});
const openChip = 'button[title="{t:chat.agentActivity.expand}"] >> nth=0';

// ----- A Harness group -----

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

const phone = { width: 390, height: 844, status: 'shipped' as const, route: chatRoute(), render: chatPhone, wait: 400 };

const designFrames = frames([
  { ...phone, name: 'phone-chip', title: '展开执行过程：面板从左上角显现，chip 原地不动 · 手机', fixtures: chip, click: [openChip] },
  { ...phone, name: 'phone-group', title: '展开 Harness 消息组：成员在组头下方显现 · 手机', fixtures: group, click: [openGroup] },
  { ...phone, name: 'phone-group-reduced', title: '展开 Harness 消息组 · 减少动态效果 · 手机', fixtures: group, click: [openGroup], reducedMotion: true },
]);

export default designFrames;
