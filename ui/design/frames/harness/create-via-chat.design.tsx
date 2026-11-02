// Harness · Create: a Task or Watch is made by describing it to an Agent in a new chat.
import { CreateViaChatDialog } from '@/components/workbench/CreateViaChatDialog';

import { frames } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';

const fixtures = workbenchFixtures();
const dialog = (kind: 'task' | 'watch') => () => <CreateViaChatDialog kind={kind} onClose={() => {}} />;

const designFrames = frames([
  { name: 'task', title: 'Harness · 和 Agent 对话来创建后台任务', width: 1000, height: 700, status: 'shipped', fixtures, render: dialog('task') },
  { name: 'watch', title: 'Harness · 和 Agent 对话来创建等待器', width: 1000, height: 700, status: 'shipped', fixtures, render: dialog('watch') },
  { name: 'phone', title: 'Harness · 和 Agent 对话来创建 · 手机', width: 390, height: 760, status: 'shipped', fixtures, render: dialog('task') },
]);

export default designFrames;
